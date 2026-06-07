import { useState, useEffect, useCallback, useMemo } from 'react'
import { Sidebar } from './components.jsx'
import { api } from './api.js'
import { parseHash, buildHash, nameOf, pathOf } from './nav.js'
import Home from './screens/Home.jsx'
import Create from './screens/Create.jsx'
import Script from './screens/Script.jsx'
import Progress from './screens/Progress.jsx'
import Remix from './screens/Remix.jsx'
import Queue from './screens/Queue.jsx'
import YouTube from './screens/YouTube.jsx'
import Library from './screens/Library.jsx'
import EditFilm from './screens/EditFilm.jsx'
import Settings from './screens/Settings.jsx'

export default function App() {
  // The URL hash is the single source of truth for which page is shown and, for
  // video-scoped pages, which video (#/edit/<slug>). Everything below derives
  // from `nav`; navigation = updating the hash (see `go`).
  const [nav, setNav] = useState(() => parseHash(window.location.hash))
  const [topic, setTopic] = useState('')          // hero quick-start text
  const [createSeed, setCreateSeed] = useState(null)  // {title, description, scenes} prefill for Create
  const [job, setJob] = useState(null)            // {job_id, work_dir, title, style, music_desc, scenes, voice, resolution}
  const [meta, setMeta] = useState({ config: {}, voices: [], resolutions: [], default_resolution: '', videos_dir: '' })
  const [badges, setBadges] = useState({})

  useEffect(() => {
    api.getConfig().then(setMeta).catch(() => {})
  }, [])

  // Keep `nav` in sync with the address bar: back/forward, manual edits, and
  // deep links all arrive as hashchange events.
  useEffect(() => {
    const onHash = () => { setNav(parseHash(window.location.hash)); window.scrollTo({ top: 0 }) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const route = nav.route
  const videosDir = meta.videos_dir || ''
  // Reconstruct the full work_dir for the page from the URL slug. Empty until
  // config (videos_dir) has loaded, or when no slug is present (active job).
  const workDir = nav.name ? pathOf(nav.name, videosDir) : ''
  // Deep-link into the YouTube tab's Publish view (#/youtube/publish/<slug>).
  // Memoized so its identity only changes when the target does (YouTube re-runs
  // an effect on `initial`).
  const ytInitial = useMemo(
    () => (route === 'youtube' && nav.view === 'publish' && workDir) ? { view: 'publish', workDir } : null,
    [route, nav.view, workDir],
  )

  // Poll the "needs attention" counts for the sidebar (render activity, queue,
  // YouTube, new films) on a light interval so the indicators stay live.
  const refreshBadges = useCallback(() => api.getBadges().then(setBadges).catch(() => {}), [])
  useEffect(() => {
    refreshBadges()
    const t = setInterval(refreshBadges, 5000)
    return () => clearInterval(t)
  }, [refreshBadges])

  const go = useCallback((id, payload) => {
    if (payload?.topic != null) setTopic(payload.topic)
    // Carry an idea's title/description/scene-count into the Create form.
    // queueItemId links the resulting script back to an existing queue slot so
    // approving it updates that slot in place instead of appending (issue #43).
    // This prefill is transient (not shareable), so it rides in state, not the URL.
    if (id === 'create') setCreateSeed({
      title: payload?.title ?? payload?.topic ?? '',
      description: payload?.description ?? '',
      scenes: payload?.scenes ?? null,
      resolution: payload?.resolution ?? '',
      queueItemId: payload?.queueItemId ?? null,
    })
    // Visiting Films marks the new ones as seen (mailbox-style clear).
    if (id === 'library') api.markSeen('films').then(refreshBadges).catch(() => {})
    // Video-scoped pages carry the work_dir basename in the URL.
    const wd = payload?.workDir ?? payload?.publishWorkDir ?? ''
    const hash = buildHash(id, { name: wd ? nameOf(wd) : undefined })
    if (window.location.hash === hash) {
      // Same URL → no hashchange fires; refresh + scroll manually.
      setNav(parseHash(hash))
      window.scrollTo({ top: 0 })
    } else {
      window.location.hash = hash
    }
  }, [refreshBadges])

  // Deep link / refresh on a Script page: hydrate the job behind the URL slug.
  // In-app navigation pre-sets `job`, so the guard skips a redundant load.
  useEffect(() => {
    if (route !== 'script' || !nav.name || !videosDir) return
    if (job && nameOf(job.work_dir) === nav.name) return
    api.loadScript(pathOf(nav.name, videosDir))
      .then((loaded) => setJob({
        ...loaded,
        voice: loaded.voice || meta.config?.default_voice || '',
        resolution: loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
      }))
      .catch(() => {})
    // `job` is intentionally omitted: the guard above prevents reload loops, and
    // re-running on every job change would fight in-app navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route, nav.name, videosDir])

  // Create → Script: a fresh script was generated. If the user opted to
  // auto-approve, launch generation immediately and jump to the render screen.
  const onScriptGenerated = useCallback(async (data, choices) => {
    const nextJob = { ...data, voice: choices.voice, voice_robotic: choices.voice_robotic, resolution: choices.resolution, queue_item_id: choices.queueItemId || '' }
    setJob(nextJob)
    if (choices.autoApprove) {
      if (data.auto_approved) {
        if (data.started) {
          go('progress', { workDir: data.started.work_dir || nextJob.work_dir })
        } else {
          go('queue')
        }
        return
      }
      try {
        const r = await api.queueFromJob({
          job_id: nextJob.job_id, work_dir: nextJob.work_dir,
          video_title: nextJob.video_title || nextJob.title || '',
          n_scenes: nextJob.scenes?.length || 0,
          style: nextJob.style || '', resolution: choices.resolution || '',
          voice: choices.voice || '', voice_robotic: choices.voice_robotic,
          music_desc: nextJob.music_desc || '',
          queue_item_id: choices.queueItemId || '',
        })
        if (r.started) go('progress', { workDir: nextJob.work_dir })
        else go('queue')
        return
      } catch {
        // fall through to manual review if enqueue failed
      }
    }
    go('script', { workDir: nextJob.work_dir })
  }, [go])

  // Script → Progress: generation launched.
  const onGenerationStarted = useCallback((workDir) => {
    go('progress', { workDir })
  }, [go])

  // Render/Films → Script: load the script behind a given work_dir and open the
  // Script tab on it. Throws so the caller can surface the error inline.
  const onOpenScript = useCallback(async (workDir) => {
    const loaded = await api.loadScript(workDir)
    setJob({
      ...loaded,
      voice: loaded.voice || meta.config?.default_voice || '',
      voice_robotic: loaded.voice_robotic ?? !!meta.config?.default_voice_robotic,
      resolution: loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
    })
    go('script', { workDir })
  }, [go, meta])

  // Queue → Script/Create: edit the script behind a pending queue item, linked
  // to its slot so "Approve → queue" updates it in place (issue #43). Items that
  // already have a ready script open straight in the Script tab; script-less
  // ones go to the Create form, prefilled, to draft a script first.
  const onEditQueueScript = useCallback(async (item) => {
    if (item.script_ready && item.work_dir && item.video_job_id) {
      const loaded = await api.loadScript(item.work_dir)
      setJob({
        ...loaded,
        voice: item.gen_voice || loaded.voice || meta.config?.default_voice || '',
        voice_robotic: item.gen_voice_robotic ?? loaded.voice_robotic ?? !!meta.config?.default_voice_robotic,
        resolution: item.gen_resolution || loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
        style: item.gen_style || loaded.style || '',
        queue_item_id: item.id,
      })
      go('script', { workDir: item.work_dir })
      return
    }
    go('create', {
      title: item.final_title || item.title || '',
      description: item.video_prompt || item.comment_text || '',
      scenes: item.suggested_scene_count || null,
      resolution: item.gen_resolution || '',
      queueItemId: item.id,
    })
  }, [go, meta])

  const screen = (() => {
    // A deep link to a video page before config has loaded can't resolve its
    // work_dir yet; wait so we open the linked video, not the active one.
    if (nav.name && !videosDir && ['script', 'progress', 'remix', 'editfilm'].includes(route)) {
      return <div style={{ padding: 40 }}>Loading…</div>
    }
    switch (route) {
      case 'home': return <Home go={go} initialTopic={topic} setTopic={setTopic} />
      case 'create': return <Create seed={createSeed} meta={meta} onGenerated={onScriptGenerated} />
      case 'script': return <Script job={job} setJob={setJob} meta={meta} onGenerate={onGenerationStarted} go={go} />
      case 'progress': return <Progress workDir={workDir} job={job} go={go} onOpenScript={onOpenScript} />
      case 'remix': return <Remix workDir={workDir} go={go} />
      case 'queue': return <Queue go={go} onEditScript={onEditQueueScript} meta={meta} />
      case 'youtube': return <YouTube go={go} initial={ytInitial} meta={meta} />
      case 'library': return <Library go={go} onOpenProgress={(wd) => go('progress', { workDir: wd })} onOpenRemix={(wd) => go('remix', { workDir: wd })} onOpenEdit={(wd) => go('editfilm', { workDir: wd })} />
      case 'editfilm': return <EditFilm workDir={workDir} go={go} />
      case 'settings': return <Settings meta={meta} setMeta={setMeta} />
      default: return <Home go={go} />
    }
  })()

  return (
    <div className="shell">
      <Sidebar route={route} go={go} badges={badges} />
      <main className="main" key={route}>{screen}</main>
    </div>
  )
}
