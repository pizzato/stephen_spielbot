import { useState, useEffect, useRef } from 'react'
import { Card, Field, Segmented, ResolutionPicker, Button, Chip, Icon, Thumb, Banner, RegenLabel, GuidedRegenButton, VersionStrip, MusicVersionStrip, InpaintModal, voiceMetaMap, voiceLabel, SceneTypeControls, ActedPrompt, isActedMode, hasActedShape, CatalogueRefCard, fmtDuration, DurationInput } from '../components.jsx'
import { api, fileUrl } from '../api.js'
import PerformanceScenes from './PerformanceScenes.jsx'
import ScriptVisuals from './ScriptVisuals.jsx'
import { styleLineage, resolveStyle } from '../styleUtils.js'

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
  // An acted scene has no narration and no still, and cites its references by
  // slot number, so it gets its own view. A film made ENTIRELY of them replaces
  // the narration-shaped Scenes tab with it; a mixed film keeps both.
  const acted = (s) => s.mode === 'dialogue' || s.mode === 'performance'
  const allActed = !!(job?.scenes || []).length && (job?.scenes || []).every(acted)
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
  const [castCatalogue, setCastCatalogue] = useState([])   // style catalogue, read-only
  const [castStyles, setCastStyles] = useState({ styles: [], defaultStyle: '' })
  useEffect(() => {
    api.getConfig().then((c) => {
      // /api/config nests the config under "config"; voices there are
      // {name,path,gender,age,accent} rows while top-level "voices" is names.
      const cfg = c?.config || c || {}
      setVoiceOpts((cfg.voices || []).map((v) => v?.name).filter(Boolean))
      setVoiceMeta(voiceMetaMap(cfg.voices))
      // Keep each character's home style so the speaker options can be
      // narrowed to the cast this job's style actually inherits.
      setGlobalCast((cfg.characters || [])
        .map((x) => ({ name: x?.name || '', style: x?.style || '' }))
        .filter((x) => x.name))
      setCastStyles({ styles: cfg.styles || [], defaultStyle: cfg.default_style || '' })
    }).catch(() => {})
  }, [])

  // Does this job's style perform its SILENT scenes on H3 (h3_silent_scenes)?
  // Those scenes are then staged exactly like the acted ones — same fields,
  // same portraits and reference images — so the editor shows them the same way.
  // (Keyed on the style NAME — see `styleKey` below, which the cast picker
  // shares; a loaded script keeps the visual-style TEXT in `style`.)
  const actedSilent = !!resolveStyle(castStyles.styles,
    job?.style_name || job?.style || castStyles.defaultStyle)?.h3_silent_scenes
  const someActed = (job?.scenes || []).some(acted)
  // Every scene that renders as an H3 take — the acted ones plus, when the
  // style performs them, the silent ones. Those takes are conditioned on
  // reference images, so this is what decides whether the film needs the
  // visuals wall (locations, wardrobe, stills) beside its characters.
  const someActedShape = (job?.scenes || []).some((s) => hasActedShape(s.mode, actedSilent, s.singing))

  // Story tab (story-first scripts only) — the prose draft the scenes come
  // from; null hides the tab (classic scripts 404 here). Chapters are editable:
  // Save persists edits into story.json (resume later); Divide turns the story
  // into scenes — in place for a fresh draft, or FORKING into a new script when
  // scenes already exist (so scene edits/previews are never clobbered).
  const [story, setStory] = useState(null)
  const [storyDrafts, setStoryDrafts] = useState({})   // chapter -> edited text
  const [storyMsg, setStoryMsg] = useState('')
  // Length control (minutes): editing it away from the story's own length
  // swaps Divide for "Redraft to N min" — a full prose retell at the new
  // length (the scene count then follows the narrator's cadence).
  const storyMinutes = (s) => s?.scene_plan?.minutes
    || Math.round(((Number(s?.n_scenes) || 6) * 9 / 60) * 100) / 100
  const [minutesTarget, setMinutesTarget] = useState('')
  const [confirmRedraft, setConfirmRedraft] = useState(false)
  useEffect(() => {
    setStory(null); setStoryDrafts({}); setStoryMsg(''); setMinutesTarget(''); setConfirmRedraft(false)
    if (job?.job_id) api.getStory(job.job_id)
      .then((s) => { setStory(s); setMinutesTarget(String(storyMinutes(s))) })
      .catch(() => setStory(null))
  }, [job?.job_id])
  // A song film's song (music-video format): caption + tagged lyrics the music
  // model sings. 404 for every other film — the tab simply doesn't appear.
  const [song, setSong] = useState(null)
  const [songDraft, setSongDraft] = useState(null)   // {caption, lyrics} while editing
  const [songMsg, setSongMsg] = useState('')
  const [songVoiceSel, setSongVoiceSel] = useState('')  // "Sing this as" voice
  // How many performed scenes the song splits into. The film runs the SONG's
  // length, so this is the only division there is — same "Scenes" control as
  // every other film, just dividing a fixed length instead of a chosen one.
  const [scenesSong, setScenesSong] = useState(0)
  const songStudioOpened = useRef(false)
  const refreshSong = () => api.getSong(job.job_id).then(setSong).catch(() => setSong(null))
  useEffect(() => {
    setSong(null); setSongDraft(null); setSongMsg(''); songStudioOpened.current = false
    if (job?.job_id) api.getSong(job.job_id).then(setSong).catch(() => setSong(null))
  }, [job?.job_id])
  // A song-first job lands here BEFORE any story or scenes exist — open
  // straight onto the studio, once.
  useEffect(() => {
    if (song && !(job?.scenes || []).length && !songStudioOpened.current) {
      songStudioOpened.current = true
      setView('song')
      if (song.voice && !songVoiceSel) setSongVoiceSel(song.voice)
    }
    // Default the split to ~5 s takes once the song's real length is known.
    if (song?.duration && !scenesSong) {
      setScenesSong(Math.max(1, Math.round(Number(song.duration) / 5)))
    }
  }, [song])
  const saveSong = async () => {
    setBusy('song-save'); setError(''); setSongMsg('')
    try {
      const s = await api.saveSong(job.job_id, (songDraft ?? song).caption, (songDraft ?? song).lyrics)
      setSong({ ...song, caption: s.caption, lyrics: s.lyrics }); setSongDraft(null)
      setSongMsg('Song saved — the next generation sings this version.')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const generateSongTrack = async () => {
    setBusy('song-gen'); setError(''); setSongMsg('')
    try {
      const cur = songDraft ?? song
      await api.songGenerate({ work_dir: job.work_dir, caption: cur.caption,
                               lyrics: cur.lyrics, voice: songVoiceSel })
      setSongDraft(null); await refreshSong()
      setSongMsg('Song generated — listen below, re-voice it, or draft the story.')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const convertSongVoice = async () => {
    setBusy('song-convert'); setError(''); setSongMsg('')
    try {
      await api.songConvert(job.work_dir, songVoiceSel)
      await refreshSong()
      setSongMsg(`Re-voiced as ${songVoiceSel} — listen and keep it, or pick an earlier version below.`)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const selectSongVersion = async (versionId) => {
    setBusy('song-select'); setError('')
    try { await api.songSelectVersion(job.job_id, versionId); await refreshSong() }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const draftStoryFromSong = async () => {
    setBusy('song-story'); setError(''); setSongMsg('')
    try {
      const b = job.create_brief || {}
      const data = await api.generateStory({
        video_title: job.video_title || job.title || '',
        topic: b.topic || job.topic || '', minutes: b.minutes || 0,
        style_name: job.style_name || '', format: 'song',
        voice: b.voice || '', resolution: b.resolution || '',
        n_scenes: Math.max(1, Number(scenesSong) || 1), work_dir: job.work_dir,
      })
      setStory(data.story || null)
      setMinutesTarget(String(storyMinutes(data.story)))
      setView('story')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const minutesTargetN = Number(minutesTarget)
  const minutesTargetOk = Number.isFinite(minutesTargetN) && minutesTargetN >= 0.25 && minutesTargetN <= 40
  const minutesTargetChanged = !!story && minutesTargetOk
    && Math.abs(minutesTargetN - storyMinutes(story)) > 0.01
  const storyChapters = () => (story?.chapters || []).map((c) => ({
    chapter: c.chapter, text: storyDrafts[c.chapter] ?? c.text,
  }))
  const saveStory = async () => {
    setBusy('story-save'); setError(''); setStoryMsg('')
    try {
      const s = await api.saveStory(job.job_id, storyChapters())
      setStory(s); setStoryDrafts({})
      setStoryMsg('Story saved — you can come back to it any time.')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const redraftStory = async () => {
    setBusy('story-redraft'); setError(''); setStoryMsg(''); setConfirmRedraft(false)
    try {
      const s = await api.redraftStory(job.job_id, {
        minutes: minutesTargetN,
        chapters: storyChapters(),
      })
      setStory(s); setStoryDrafts({}); setMinutesTarget(String(storyMinutes(s)))
      setStoryMsg(`Story redrafted for ${fmtDuration(storyMinutes(s))} (${s.n_scenes} scenes) — review it, then divide into scenes.`)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const divideStory = async () => {
    setBusy('story-divide'); setError(''); setStoryMsg('')
    try {
      const data = await api.divideStory({
        work_dir: job.work_dir,
        chapters: storyChapters(),
        voice: job.voice || '',
        resolution: job.resolution || '',
        style_name: job.style_name || '',
        queue_item_id: job.queue_item_id || '',
        // Chosen at Create: skip the scene review and queue it once divided.
        auto_approve: !!job.create_brief?.auto_approve,
      })
      if (data.auto_approved) {
        go(data.started ? 'progress' : 'queue',
           data.started ? { workDir: data.started.work_dir || data.work_dir } : undefined)
        return
      }
      const forked = data.job_id !== job.job_id
      setJob({ ...job, ...data })
      if (!forked) {
        // same job: the job-load effect won't re-run, sync by hand
        setScenes(data.scenes || [])
        setCharacters(data.characters || [])
        api.getStory(data.job_id).then(setStory).catch(() => {})
        setView('scenes')
      }
      refreshScripts()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  // Script critic (post-generation QC): rewrites weak narrations, deletes
  // redundant scenes, adds bridging scenes, reorders for flow. Run 1..5 passes
  // or until the critic proposes nothing (converged, capped server-side). The
  // script is snapshotted before every applied pass — restorable below.
  const [criticBusy, setCriticBusy] = useState(false)
  const [criticMsg, setCriticMsg] = useState('')
  const [criticPasses, setCriticPasses] = useState('1')   // '1'..'5' | 'auto'
  const [versions, setVersions] = useState([])
  const [versionSel, setVersionSel] = useState('')
  const refreshVersions = (jobId) => api.listScriptVersions(jobId)
    .then((d) => { setVersions(d.versions || []); setVersionSel((d.versions || [])[0]?.file || '') })
    .catch(() => setVersions([]))
  useEffect(() => { if (job?.job_id) refreshVersions(job.job_id); else setVersions([]) }, [job?.job_id])
  const runCritic = async () => {
    setCriticBusy(true); setError(''); setCriticMsg('')
    try {
      const r = await api.runCritic(job.job_id, {
        passes: criticPasses === 'auto' ? 1 : Number(criticPasses),
        untilConverged: criticPasses === 'auto',
      })
      setScenes(r.scenes || [])
      setCur((c) => Math.max(0, Math.min(c, (r.scenes || []).length - 1)))
      setVersions(r.versions || []); setVersionSel((r.versions || [])[0]?.file || '')
      const sum = (k) => r.passes.reduce((n, p) => n + (Array.isArray(p[k]) ? p[k].length : (p[k] || 0)), 0)
      const rewrites = sum('rewrites'), deleted = sum('deleted'), added = sum('added')
      const reordered = r.passes.some((p) => p.reordered)
      const parts = []
      if (rewrites) parts.push(`${rewrites} narration${rewrites === 1 ? '' : 's'} rewritten`)
      if (deleted) parts.push(`${deleted} scene${deleted === 1 ? '' : 's'} deleted`)
      if (added) parts.push(`${added} scene${added === 1 ? '' : 's'} added`)
      if (reordered) parts.push('scenes reordered')
      if (!parts.length) parts.push('no changes needed')
      const notes = r.passes[r.passes.length - 1]?.notes || []
      setCriticMsg(`Critic — ${r.passes.length} pass${r.passes.length === 1 ? '' : 'es'}`
        + `${r.converged ? ', converged' : ''}: ${parts.join(', ')}.`
        + (notes.length ? ` “${notes[0]}”` : ''))
    } catch (e) { setError(e.message) } finally { setCriticBusy(false) }
  }
  const restoreVersion = async () => {
    if (!versionSel) return
    setCriticBusy(true); setError(''); setCriticMsg('')
    try {
      const r = await api.restoreScriptVersion(job.job_id, versionSel)
      setScenes(r.scenes || [])
      setCur(0)
      setVersions(r.versions || [])
      setVersionSel((r.versions || [])[0]?.file || '')
      setCriticMsg('Script restored to the selected version (the pre-restore state was saved too).')
    } catch (e) { setError(e.message) } finally { setCriticBusy(false) }
  }

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
  const [confirmDelScene, setConfirmDelScene] = useState(false)

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
    // A story draft (no scenes yet) opens straight into the Story view for
    // review + division; anything with scenes lands on Cover as before.
    if (!job?.job_id) return
    const hasScenes = (job.scenes || []).length
    setView(hasScenes ? 'cover' : 'story')
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

  // Generate any missing scene previews as soon as the script loads.
  // An acted scene renders no first frame — it is conditioned on the character
  // portraits instead — so painting a still for one is pure wasted GPU on a
  // frame the film never looks at. A mixed film still needs its narrated ones.
  useEffect(() => {
    if (!job?.job_id || allActed) return
    if (!(job.scenes || []).some((s) => !s.has_preview && !acted(s))) return
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

  // The job snapshot's cast can be stale: story division persists the
  // identified characters server-side WITHOUT changing job_id (so the sync
  // effect above never re-seeds), and background passes keep adding looks.
  // Re-pull the list from the server whenever the tab is opened.
  useEffect(() => {
    // Not gated on the Characters tab: the Scenes tab's References card needs
    // the catalogue portraits too (most casts are catalogue members).
    if (!job?.job_id) return
    let alive = true
    api.scriptCharacters(job.job_id)
      .then((r) => { if (alive) { setCharacters(r.characters || []); setCastCatalogue(r.catalogue || []) } })
      .catch(() => { /* keep the snapshot */ })
    return () => { alive = false }
  }, [view, job?.job_id])

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
      const r = await api.saveScene(job.job_id, s.id, {
        title: s.title || '', image_prompt: s.image_prompt || '',
        video_prompt: s.video_prompt || '', narration: s.narration || '',
        tts_text: s.tts_text || '',
        mode: s.mode || 'narration', lines: s.lines || [], duration: s.duration || 0,
        // Acted-scene fields — the server assembles the video prompt from these.
        setting: s.setting ?? null, camera: s.camera ?? null,
        soundscape: s.soundscape ?? null, cast: s.cast ?? null,
        beats: s.beats ?? null, seconds: s.seconds ?? null,
      })
      // The server rebuilt the prompt (and narration) from the fields — adopt
      // its copy so the read-only prompt on screen is exactly what renders.
      const fresh = (r?.scene && r.scene.id === s.id) ? r.scene : null
      if (fresh) setScenes((arr) => arr.map((x, i) => (i === idx ? { ...x, ...fresh } : x)))
    } catch (e) { setError(e.message) }
  }

  // Pin / unpin the assembled prompt for an acted scene.
  const savePromptOverride = async (text) => {
    const s = scenes[cur]
    if (!s) return
    try {
      const r = await api.saveScene(job.job_id, s.id, {
        title: s.title || '',
        // A dialogue scene renders no image; a performed silent one opens on the
        // frame its image prompt paints, so pinning a prompt must not wipe it.
        image_prompt: isActedMode(s.mode) ? '' : (s.image_prompt || ''),
        video_prompt: s.video_prompt || '',
        narration: s.narration || '', mode: s.mode || 'dialogue', prompt: text,
      })
      const fresh = (r?.scene && r.scene.id === s.id) ? r.scene : null
      if (fresh) setScenes((arr) => arr.map((x, i) => (i === cur ? { ...x, ...fresh } : x)))
    } catch (e) { setError(e.message) }
  }

  // Dialogue speakers: the script's own cast plus the catalogue characters the
  // job's style inherits — the global pool and its style lineage (mirrors the
  // backend's _style_characters; the resolver falls back to catalogue
  // portraits/voices). The shot editing itself lives in SceneTypeControls.
  // Keyed on the style's NAME: a script loaded from disk keeps that in
  // `style_name` and its visual-style TEXT in `style`, and matching the text
  // against the hierarchy offered none of the style's own cast.
  const styleKey = job?.style_name || job?.style || castStyles.defaultStyle
  const lineageNames = new Set(styleLineage(castStyles.styles, styleKey).map((s) => s.name))
  // "(none)" imposes no STYLE cast but still sees the global pool — mirrors
  // the backend's _style_characters, whose resolver casts those at render.
  const styleCast = styleKey === '(none)'
    ? globalCast.filter((x) => !x.style).map((x) => x.name)
    : globalCast.filter((x) => !x.style || lineageNames.has(x.style)).map((x) => x.name)
  const castOpts = [...new Set([...characters.map((c) => c.name), ...styleCast])].filter(Boolean)

  const move = async (to) => {
    if (to < 0 || to >= total) return
    setConfirmDelScene(false)
    await persist(cur)
    setCur(to)
  }

  // Structural edits (issue #193): the backend renumbers scene ids to 1..N and
  // returns the fresh list, so replace state wholesale. Every thumb gets a new
  // cache-buster — renaming scene files reuses previously-served URLs.
  const applyStructure = (r, focusIdx) => {
    const fresh = (r.scenes || []).map((s) => ({ ...s, cb: Date.now() }))
    setScenes(fresh)
    setCur(Math.max(0, Math.min(focusIdx, fresh.length - 1)))
    setConfirmDelScene(false)
  }

  const addSceneAfter = async () => {
    setBusy('structure'); setError('')
    try {
      await persist(cur)
      const r = await api.addScene(job.job_id, d.id || 0)
      applyStructure(r, cur + 1)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const deleteScene = async () => {
    setBusy('structure'); setError('')
    try {
      const r = await api.deleteScene(job.job_id, d.id)
      applyStructure(r, cur)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const moveScene = async (di) => {
    const to = cur + di
    if (to < 0 || to >= total) return
    setBusy('structure'); setError('')
    try {
      await persist(cur)
      const order = scenes.map((s) => s.id)
      ;[order[cur], order[to]] = [order[to], order[cur]]
      const r = await api.reorderScenes(job.job_id, order)
      applyStructure(r, to)
    } catch (e) { setError(e.message) } finally { setBusy('') }
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
        minutes: job.create_brief?.minutes || 0,
        style, resolution, voice: job.voice || '',
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
    setCharMsg(job?.style && job.style !== '(none)'
      ? `Saved “${c.name || 'character'}” to the “${job.style}” cast — that style and its child styles can now reuse it.`
      : `Saved “${c.name || 'character'}” to the global character pool.`)
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
                  minutes: b.minutes || null,
                  scenes: b.n_scenes || job.scenes?.length || null,
                  resolution: b.resolution || job.resolution || '',
                  styleName: b.style_name || job.style_name || '',
                  voice: b.voice || job.voice || '',
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
            <>
              <select className="select" value={criticPasses} disabled={criticBusy}
                onChange={(e) => setCriticPasses(e.target.value)}
                style={{ width: 130 }} title="How many critic passes to run">
                <option value="1">1 pass</option>
                <option value="2">2 passes</option>
                <option value="3">3 passes</option>
                <option value="5">5 passes</option>
                <option value="auto">Until stable</option>
              </select>
              <Button variant="ghost" icon="gavel" disabled={criticBusy || busy === 'generate'}
                onClick={runCritic}>{criticBusy ? 'Critiquing…' : 'Run critic'}</Button>
              <Button variant="primary" iconRight="layer-group" disabled={busy === 'generate'}
                onClick={approve}>{busy === 'generate' ? 'Approving…' : job.queue_item_id ? '2. Save to queue slot' : '2. Approve → queue'}</Button>
            </>
          )}

      {view === 'performance' && job && (
            <Button variant="primary" iconRight="layer-group" disabled={busy === 'generate'}
              onClick={approve}>{busy === 'generate' ? 'Approving…' : job.queue_item_id ? 'Save to queue slot' : 'Approve → render'}</Button>
          )}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {job?.queue_item_id && view === 'scenes' && <Banner tone="info">Editing a queued request — “Save to queue slot” keeps its position and lets it render straight from this script.</Banner>}
      {genAll && <Banner tone="info">{genAllMsg}</Banner>}
      {!genAll && regenStatus && <Banner tone="ok">{regenStatus}</Banner>}
      {view === 'cover' && coverMsg && <Banner tone="ok">{coverMsg}</Banner>}
      {view === 'scenes' && criticMsg && <Banner tone="ok">{criticMsg}</Banner>}
      {view === 'scenes' && criticBusy && <Banner tone="info">The critic is reading the whole script — checking consistency, repetition, and engagement…</Banner>}
      {view === 'scenes' && job && versions.length > 0 && (
        <div className="row center gap-10 reveal" style={{ marginBottom: 16 }}>
          <span className="label-sm">Script history</span>
          <select className="select" value={versionSel} disabled={criticBusy}
            onChange={(e) => setVersionSel(e.target.value)} style={{ maxWidth: 340 }}>
            {versions.map((v) => (
              <option key={v.file} value={v.file}>
                {(v.label || 'snapshot')} — {new Date((v.saved_at || 0) * 1000).toLocaleString()} ({v.scene_count} scenes)
              </option>
            ))}
          </select>
          <Button variant="ghost" icon="clock-rotate-left" disabled={criticBusy || !versionSel}
            onClick={restoreVersion}>Restore</Button>
        </div>
      )}
      {view === 'characters' && charMsg && <Banner tone="ok">{charMsg}</Banner>}

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={(v) => { setView(v); setError('') }} options={[
          { value: 'scripts', label: 'Scripts' },
          ...(story || (job && !(job.scenes || []).length) ? [{ value: 'story', label: 'Story' }] : []),
          ...(song ? [{ value: 'song', label: 'Song' }] : []),
          { value: 'cover', label: 'Cover' },
          // ONE look whatever the mix: the Scenes editor (where every scene
          // can shift mode) plus, when anything is acted, the Acted scenes
          // view with its cast slots, takes and prompts.
          { value: 'characters', label: someActedShape ? 'Characters & Artifacts' : 'Characters' },
          { value: 'scenes', label: 'Scenes' },
          ...(someActedShape ? [{ value: 'performance', label: 'Acted scenes' }] : []),
        ]} />
      </div>

      {/* ── Performance tab: scenes, their numbered references, and the prompt ── */}
      {view === 'performance' && job && (
        <PerformanceScenes workDir={job.work_dir} jobId={job.job_id}
          voiceOpts={voiceOpts} voiceMeta={voiceMeta} />
      )}

      {/* ── Song tab (music-video films): the words and sound the film sings ── */}
      {view === 'song' && song && (
        <div className="bento">
          <Card span={8} padLg className="reveal reveal-d1">
            <div className="stack gap-22">
              {songMsg && <Banner tone="ok">{songMsg}</Banner>}
              <Field label="Sound"
                hint="What the music model is told about the song — genre, tempo, mood, arrangement. The lead performer's cast voice (gender, age, tone) is described automatically on top of this at render time.">
                <textarea className="textarea" rows={3}
                  value={(songDraft ?? song).caption}
                  onChange={(e) => setSongDraft({ ...(songDraft ?? song), caption: e.target.value })} />
              </Field>
              <Field label="Lyrics"
                hint="Sung exactly as written. Keep the section tags — [Intro], [Verse], [Chorus], [Bridge], [Outro] — on their own lines; they shape the song without being sung. The song runs the film's length, so cutting or adding many words changes how it fits.">
                <textarea className="textarea" rows={18} style={{ fontFamily: 'ui-monospace, monospace' }}
                  value={(songDraft ?? song).lyrics}
                  onChange={(e) => setSongDraft({ ...(songDraft ?? song), lyrics: e.target.value })} />
              </Field>
              <div className="row gap-8">
                <Button variant="ghost" disabled={!songDraft || !!busy}
                  onClick={saveSong}>{busy === 'song-save' ? 'Saving…' : 'Save edits'}</Button>
                {songDraft && <Button variant="quiet" onClick={() => setSongDraft(null)}>Discard edits</Button>}
              </div>

              <Field label="Singing voice"
                hint="Steers the described vocalist at generation, and is the target for re-voicing below.">
                <select className="select" style={{ maxWidth: 340 }} value={songVoiceSel}
                  disabled={!!busy} onChange={(e) => setSongVoiceSel(e.target.value)}>
                  <option value="">The model’s own vocalist</option>
                  {voiceOpts.map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMeta)}</option>)}
                </select>
              </Field>

              <div className="row gap-12 center row--wrap">
                <Button variant={song.song_url ? 'ghost' : 'primary'} icon="music"
                  disabled={!!busy || !(songDraft ?? song).lyrics?.trim()}
                  onClick={generateSongTrack}>
                  {busy === 'song-gen' ? 'Singing it…' : song.song_url ? 'Generate again' : '1. Generate the song'}
                </Button>
                {song.song_url && !!songVoiceSel && (
                  <Button variant="ghost" icon="microphone-lines" disabled={!!busy}
                    onClick={convertSongVoice}>
                    {busy === 'song-convert' ? 'Re-voicing…' : `2. Sing this as ${songVoiceSel}`}
                  </Button>
                )}
                {song.sung_as && <span className="muted" style={{ fontSize: 12.5 }}>♪ currently sung as <strong>{song.sung_as}</strong></span>}
              </div>
              {song.song_url && (
                <audio controls src={song.song_url} style={{ width: '100%', height: 36 }} />
              )}

              {/* Accept or go back: every generation and every re-voicing is a
                  kept version — the one marked "In use" is the film's track. */}
              <MusicVersionStrip versions={song.versions || []} selected={song.selected}
                busy={busy === 'song-select'} onSelect={selectSongVersion} />

              {song.song_url && (
                <div className="row center between row--wrap gap-12 mt-8">
                  <Field label="Scenes"
                    hint={(() => {
                      const dur = Number(song.duration) || 0
                      const n = Math.max(1, Number(scenesSong) || 1)
                      return dur
                        ? `The film runs the song's length (${dur.toFixed(0)} s), split ${n} way${n === 1 ? '' : 's'} — ${n} scene${n === 1 ? '' : 's'} of ~${(dur / n).toFixed(1)} s, each performed against its own stretch of the track. Fewer scenes are longer takes.`
                        : 'How many performed scenes the song is split into — each is generated against its own stretch of the track.'
                    })()}>
                    <input className="input" type="number" min={1} max={200} step={1}
                      style={{ width: 100 }} value={scenesSong}
                      onChange={(e) => setScenesSong(e.target.value)} />
                  </Field>
                  <Button variant="primary" size="lg" iconRight="wand-magic-sparkles"
                    disabled={!!busy}
                    onClick={draftStoryFromSong}>
                    {busy === 'song-story' ? 'Drafting the story…' : '3. Draft the story →'}
                  </Button>
                </div>
              )}
            </div>
          </Card>
          <Card span={4} well className="reveal reveal-d2">
            <span className="label-sm">The song studio</span>
            <p className="muted mt-8" style={{ fontSize: 12.5, lineHeight: 1.55 }}>
              This film is a <strong>music video</strong> and the song leads: generate it,
              listen, re-voice it as a library voice (an actual clone — only the vocal stem
              is converted, the instruments stay untouched), and pick the version you want
              from the list. Only then is the story drafted from these lyrics, and every
              scene performs its own stretch of this exact track. It also appears under
              Characters &amp; Artifacts, since it is an input of every singing take.
            </p>
          </Card>
        </div>
      )}

      {/* ── Story tab (story-first scripts): the prose behind the scenes ─────── */}
      {view === 'story' && !story && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>Loading the story draft…</p></Card>
      )}
      {view === 'story' && story && (
        <div className="bento">
          <Card span={8} padLg className="reveal reveal-d1">
            <div className="stack gap-22">
              {storyMsg && <Banner tone="ok">{storyMsg}</Banner>}
              {busy === 'story-redraft' && (
                <Banner tone="info">Redrafting the story to {fmtDuration(minutesTargetN)} — every chapter is being rewritten, this takes a while…</Banner>
              )}
              {(story.chapters || []).map((c) => (
                <Field key={c.chapter}
                  label={(story.chapters.length > 1 ? `Chapter ${c.chapter} — ` : '') + (c.title || '') + ` (${c.scenes} scene${c.scenes === 1 ? '' : 's'})`}>
                  <textarea className="textarea"
                    rows={Math.min(12, Math.max(4, Math.ceil(((storyDrafts[c.chapter] ?? c.text) || '').length / 90)))}
                    value={storyDrafts[c.chapter] ?? c.text ?? ''}
                    onChange={(e) => setStoryDrafts((m) => ({ ...m, [c.chapter]: e.target.value }))} />
                </Field>
              ))}
              <div className="row center between mt-8 row--wrap gap-16">
                <Button variant="ghost" icon="floppy-disk" disabled={!!busy} onClick={saveStory}>
                  {busy === 'story-save' ? 'Saving…' : 'Save story'}
                </Button>
                <div className="row center gap-10 row--wrap">
                  <span className="label-sm">Length</span>
                  <DurationInput value={minutesTarget} disabled={!!busy}
                    onChange={(v) => { setMinutesTarget(v); setConfirmRedraft(false) }} />
                  {!minutesTargetChanged ? (
                    <Button variant="primary" size="lg" iconRight="scissors" disabled={!!busy} onClick={divideStory}>
                      {busy === 'story-divide'
                        ? 'Dividing into scenes…'
                        : (scenes.length
                          ? 'Divide again → new script'
                          : `Divide into ${story.n_scenes || '?'} scenes →`)}
                    </Button>
                  ) : confirmRedraft ? (
                    <>
                      <Button variant="danger" icon="wand-magic-sparkles" disabled={!!busy} onClick={redraftStory}>
                        {busy === 'story-redraft' ? 'Redrafting…' : `Confirm — rewrite for ${fmtDuration(minutesTargetN)}`}
                      </Button>
                      <Button variant="ghost" disabled={!!busy} onClick={() => setConfirmRedraft(false)}>Cancel</Button>
                    </>
                  ) : (
                    <Button variant="primary" size="lg" iconRight="wand-magic-sparkles"
                      disabled={!!busy} onClick={() => setConfirmRedraft(true)}>
                      {`Redraft to ${fmtDuration(minutesTargetN)}…`}
                    </Button>
                  )}
                </div>
              </div>
            </div>
          </Card>
          <div className="col-4 stack gap-16">
            <Card well className="reveal reveal-d2">
              <span className="label-sm">AI editor verdict</span>
              <p className="muted mt-8" style={{ fontSize: 12.5 }}>
                {story.critique?.verdict === 'pass' && 'Reviewed: coherent, no repetition flagged.'}
                {story.critique?.verdict === 'revise' && 'Issues were flagged and the draft revised before scene division.'}
                {story.critique?.verdict === 'skipped' && 'The critique step was skipped (it failed or was unavailable).'}
              </p>
              {(story.critique?.notes || []).length > 0 && (
                <ul className="muted" style={{ fontSize: 12.5, paddingLeft: 18, marginTop: 8 }}>
                  {story.critique.notes.map((n, i) => <li key={i}>{n}</li>)}
                </ul>
              )}
            </Card>
            <Card well className="reveal reveal-d3">
              <div className="row center gap-10">
                <Icon name="circle-info" style={{ color: 'var(--ink-3)' }} />
                <span className="muted" style={{ fontSize: 12.5 }}>
                  {minutesTargetChanged
                    ? `Redrafting rewrites the WHOLE prose story to run ${fmtDuration(minutesTargetN)} — the current draft is replaced and you'll review + divide again afterwards.`
                      + (scenes.length ? ' Existing scenes stay untouched; dividing later forks into a new script.' : '')
                    : scenes.length
                      ? 'This script already has scenes, so dividing again forks the edited story into a NEW script — the current scenes stay untouched. Change the length to redraft the story longer or shorter first.'
                      : 'Edit freely and Save to come back later — the draft is kept until you divide it into scenes. Change the length to redraft the story longer or shorter.'}
                </span>
              </div>
            </Card>
          </div>
        </div>
      )}

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
                <span className="row center gap-8">
                  {s.story_draft && <Chip dot>Story draft</Chip>}
                  {job?.work_dir === s.work_dir && <Chip tone="ok" dot>Loaded</Chip>}
                </span>
              </div>
              <div className="row gap-10 mt-16 row--wrap">
                <Button variant="primary" icon="folder-open" disabled={!!busy} onClick={() => loadScript(s.work_dir)}>
                  {busy === 'load:' + s.work_dir ? 'Loading…' : job?.work_dir === s.work_dir ? 'Reload' : 'Load'}
                </Button>
                {!s.story_draft && (
                  <Button variant="ghost" icon="copy" disabled={!!busy} onClick={() => duplicateScript(s.work_dir)}>
                    {busy === 'dup:' + s.work_dir ? 'Duplicating…' : 'Duplicate'}
                  </Button>
                )}
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
                <div className="row center gap-8">
                  <Button variant="quiet" icon="arrow-left" title="Move this scene earlier"
                    disabled={cur === 0 || !!busy} onClick={() => moveScene(-1)} />
                  <Button variant="quiet" icon="arrow-right" title="Move this scene later"
                    disabled={cur >= total - 1 || !!busy} onClick={() => moveScene(1)} />
                  <Button variant="ghost" icon="plus" disabled={!!busy} onClick={addSceneAfter}>
                    {busy === 'structure' ? 'Working…' : 'Add scene'}
                  </Button>
                  {confirmDelScene ? (
                    <>
                      <Button variant="danger" icon="trash-can" disabled={!!busy} onClick={deleteScene}>Confirm delete</Button>
                      <Button variant="ghost" disabled={!!busy} onClick={() => setConfirmDelScene(false)}>Cancel</Button>
                    </>
                  ) : (
                    <Button variant="quiet" icon="trash-can" title="Delete this scene"
                      disabled={total <= 1 || !!busy} onClick={() => setConfirmDelScene(true)} />
                  )}
                  <Chip tone="accent" dot>{(() => {
                    // Spoken length of THIS scene at the script's cadence plan
                    // (words ÷ wpm) — the 10–15s contract made visible.
                    const wpm = job?.create_brief?.scene_plan?.wpm || 150
                    const words = String(d.narration || '').split(/\s+/).filter(Boolean).length
                    return words ? `~${Math.max(1, Math.round(words / wpm * 60))}s` : '~12s'
                  })()}</Chip>
                </div>
              </div>

              <div className="stack gap-22 mt-24">
                <Field label={fieldLabel('Scene title', 'title')}>
                  <input className="input" value={d.title || ''} onChange={(e) => setField('title', e.target.value)} onBlur={() => persist(cur)} />
                </Field>

                <SceneTypeControls scene={d} castOpts={castOpts} actedSilent={actedSilent}
                  onChange={(patch, commit) => { patchScene(patch); if (commit) persist(cur, { ...scenes[cur], ...patch }) }}
                  onCommit={() => persist(cur)}
                  onConvert={async (m) => {
                    setError('')
                    try {
                      const r = await api.convertSceneMode(job.job_id, d.id, m)
                      if (r?.scene) setScenes((arr) => arr.map((x, i) => (i === cur ? { ...x, ...r.scene } : x)))
                    } catch (e) { setError(e.message) }
                  }} />

                {(d.mode || 'narration') === 'narration' && (
                  <Field label={fieldLabel('Narration', 'narration', 'microphone-lines')}>
                    <textarea className="textarea" rows={4} value={d.narration || ''} onChange={(e) => setField('narration', e.target.value)} onBlur={() => persist(cur)} />
                  </Field>
                )}

                {(d.mode || 'narration') === 'narration' && (d.tts_text || '').trim() && (
                  <div className="muted" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
                    <Icon name="microphone-lines" /> Spoken text is split from the narration (set on the film's
                    edit screen) — the voice reads: “{d.tts_text}”{' '}
                    <button type="button" style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', textDecoration: 'underline', cursor: 'pointer', fontSize: 'inherit' }}
                      onClick={() => { const s = { ...scenes[cur], tts_text: '' }; setField('tts_text', ''); persist(cur, s) }}>
                      Unsplit — speak the narration
                    </button>
                  </div>
                )}

                {isActedMode(d.mode) ? (
                  <>
                    <div>
                      <GuidedRegenButton variant="ghost" icon="rotate-right"
                        label="Re-generate scene" busyLabel="Rewriting…"
                        busy={busy === 'acted-regen'} disabled={!!busy}
                        chips={['Funnier', 'Simpler words', 'More back-and-forth', 'Different setting']}
                        onRegen={async (instr) => {
                          setBusy('acted-regen'); setError('')
                          try {
                            const r = await api.regenActedScene(job.job_id, d.id, instr)
                            if (r?.scene) setScenes((arr) => arr.map((x, i) => (i === cur ? { ...x, ...r.scene } : x)))
                          } catch (e) { setError(e.message) } finally { setBusy('') }
                        }} />
                      <div className="muted mt-8" style={{ fontSize: 12 }}>
                        Rewrites the whole take — dialogue, action, setting — and rebuilds the prompt.
                      </div>
                    </div>
                    <ActedPrompt prompt={d.video_prompt || ''} edited={!!d.prompt_edited}
                    refs={(d.cast || []).map((n, i) => ({ slot: i + 1, name: n }))}
                    onSave={(text) => savePromptOverride(text)}
                    onRebuild={() => savePromptOverride('')} />
                  </>
                ) : (
                  <>
                    <Field label={fieldLabel('Image prompt', 'image_prompt', 'image')}
                      hint={hasActedShape(d.mode, actedSilent, d.singing)
                        ? 'FLUX — the frame this take opens on.'
                        : 'FLUX — static, highly detailed.'}>
                      <textarea className="textarea" rows={4} value={d.image_prompt || ''} onChange={(e) => setField('image_prompt', e.target.value)} onBlur={() => persist(cur)} />
                    </Field>
                    <Field label={fieldLabel('Video prompt', 'video_prompt', 'film')}
                      hint={hasActedShape(d.mode, actedSilent, d.singing)
                        ? 'Stands in as the setting while the Setting field above is empty.'
                        : 'For the video engine (LTX / MiniMax H3) — motion & camera.'}>
                      <textarea className="textarea" rows={5} value={d.video_prompt || ''} onChange={(e) => setField('video_prompt', e.target.value)} onBlur={() => persist(cur)} />
                    </Field>
                    {/* The performed silent take's own H3 prompt, assembled from
                        the fields above — the same view a dialogue scene gets. */}
                    {hasActedShape(d.mode, actedSilent, d.singing) && (
                      <ActedPrompt label="Acted prompt" prompt={d.acted_prompt || ''} edited={!!d.prompt_edited}
                        refs={(d.cast || []).map((n, i) => ({ slot: i + 1, name: n }))}
                        onSave={(text) => savePromptOverride(text)}
                        onRebuild={() => savePromptOverride('')} />
                    )}
                  </>
                )}
              </div>
            </Card>

            <div className="col-4 stack gap-16">
              {hasActedShape(d.mode, actedSilent, d.singing) ? (
                <Card className="reveal reveal-d2">
                  <span className="label-sm">References</span>
                  <div className="muted mt-8" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
                    {isActedMode(d.mode)
                      ? 'An acted scene renders from these references.'
                      : 'This silent beat is performed on H3 — it renders from these references and its first frame.'}
                  </div>
                  <div className="row gap-10 row--wrap mt-16">
                    {(d.cast || []).map((n, i) => {
                      const c = characters.find((x) => x.name === n) || castCatalogue.find((x) => x.name === n)
                      return (
                        <div key={n} className="stack gap-4" style={{ width: 86, textAlign: 'center' }}>
                          {c?.image_url
                            ? <img src={c.image_url} alt={n} style={{ width: 86, height: 86, objectFit: 'cover', borderRadius: 10, border: '1px solid var(--line)' }} />
                            : <div style={{ width: 86, height: 86, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--paper-2)', border: '1px dashed var(--line)' }}><Icon name="user" /></div>}
                          <span className="muted" style={{ fontSize: 11 }}>Picture {i + 1} · {n}</span>
                        </div>
                      )
                    })}
                    {!(d.cast || []).length && <span className="muted" style={{ fontSize: 12.5 }}>No one on screen yet — pick the cast on the left.</span>}
                  </div>
                  <div className="muted mt-16" style={{ fontSize: 12 }}>
                    Portraits, voices, and the scenery &amp; wardrobe reference images are
                    managed in <strong>Characters &amp; artifacts</strong>.
                  </div>
                </Card>
              ) : null}
              <Card className="reveal reveal-d2">
                <span className="label-sm">First frame</span>
                {isActedMode(d.mode) && (
                  <div className="muted mt-8" style={{ fontSize: 12, lineHeight: 1.5 }}>
                    Optional for an acted scene: painted from the <strong>setting</strong> with
                    the cast anchored to their portraits, and passed to the take as its
                    opening-composition reference — it anchors the space and framing
                    (faces and voices still come from their own references).
                  </div>
                )}
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
                {isActedMode(d.mode) && d.has_preview && (
                  <Button variant="ghost" block icon="trash-can" disabled={!!busy}
                    onClick={async () => {
                      setError('')
                      try {
                        await api.removeScenePreview(job.job_id, d.id)
                        setScenes((arr) => arr.map((x, i) => (i === cur ? { ...x, has_preview: false, preview_path: '' } : x)))
                      } catch (e) { setError(e.message) }
                    }}>Remove first frame</Button>
                )}
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

          {someActedShape ? (
            <ScriptVisuals jobId={job.job_id}
              onAddCharacter={addCharacter} addingCharacter={charBusy === 'add'}
              sceneIds={(job.scenes || []).map((s) => s.id)}
              castNames={[...new Set([...castCatalogue.map((c) => c.name),
                // Whoever is on screen anywhere — a silent take's cast never
                // speaks, so the lines alone would hide them from "worn by".
                ...(job.scenes || []).flatMap((s) => s.cast || []),
                ...(job.scenes || []).flatMap((s) => (s.lines || []).map((l) => l.speaker))].filter(Boolean))]}
              settingHint={(job.scenes || []).map((s) => s.setting || s.metadata?.setting).find(Boolean) || ''} />
          ) : (
            <Card span={12} well>
              <div className="row between center">
                <span className="muted" style={{ fontSize: 13 }}>The people this film keeps consistent.</span>
                <Button variant="primary" size="sm" icon="user-plus" disabled={!!charBusy}
                  onClick={addCharacter}>{charBusy === 'add' ? 'Adding…' : 'Add character'}</Button>
              </div>
            </Card>
          )}

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
                    <Field label="Voice" hint={someActed
                      ? 'Passed to the video model as this character\u2019s <Audio N> reference so they sound the same in every scene. Leave it unset and the model invents a voice \u2014 which will drift between scenes.'
                      : 'Cloned voice this character speaks with in dialogue scenes.'}>
                      <select className="input" value={c.voice || ''} disabled={b}
                        onChange={(e) => setCharVoice(c, e.target.value)}>
                        <option value="">{someActed
                          ? 'Let the model invent the voice (no reference)'
                          : 'Style narrator (default)'}</option>
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

          {/* Catalogue members and the film's locations & wardrobe sit at the
              SAME level as the script's own characters — one reference wall. */}
          {castCatalogue.map((c) => (
            <CatalogueRefCard key={`cat-${c.id || c.name}`} name={c.name} kind="Character"
              description={c.description} imageUrl={c.image_url} icon="user"
              voiceName={c.voice} voiceUrl={c.voice_url}
              editHint="Settings → Characters" />
          ))}
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
