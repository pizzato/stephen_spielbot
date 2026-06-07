import React, { useState, useEffect, useCallback } from 'react'
import { Sidebar } from './components.jsx'
import { api } from './api.js'
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
  const [route, setRoute] = useState('home')
  const [topic, setTopic] = useState('')          // hero quick-start text
  const [createSeed, setCreateSeed] = useState(null)  // {title, description, scenes} prefill for Create
  const [job, setJob] = useState(null)            // {job_id, work_dir, title, style, music_desc, scenes, voice, resolution}
  const [progressDir, setProgressDir] = useState('')
  const [editFilmDir, setEditFilmDir] = useState('')
  const [meta, setMeta] = useState({ config: {}, voices: [], resolutions: [], default_resolution: '' })
  const [badges, setBadges] = useState({})
  const [ytInitial, setYtInitial] = useState(null)   // {view, workDir} to deep-link the YouTube tab

  useEffect(() => {
    api.getConfig().then(setMeta).catch(() => {})
  }, [])

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
    if (id === 'create') setCreateSeed({
      title: payload?.title ?? payload?.topic ?? '',
      description: payload?.description ?? '',
      scenes: payload?.scenes ?? null,
      resolution: payload?.resolution ?? '',
      queueItemId: payload?.queueItemId ?? null,
    })
    // Deep-link into the YouTube tab's Publish view (e.g. from a Films card).
    if (id === 'youtube') setYtInitial(payload?.publishWorkDir ? { view: 'publish', workDir: payload.publishWorkDir } : null)
    // Navigate to a specific render or reset to the active render (empty = backend resolves).
    if (id === 'progress' || id === 'remix') setProgressDir(payload?.workDir ?? '')
    if (id === 'editfilm') setEditFilmDir(payload?.workDir ?? '')
    setRoute(id)
    window.scrollTo({ top: 0 })
    // Visiting Films marks the new ones as seen (mailbox-style clear).
    if (id === 'library') api.markSeen('films').then(refreshBadges).catch(() => {})
  }, [refreshBadges])

  // Create → Script: a fresh script was generated. If the user opted to
  // auto-approve, launch generation immediately and jump to the render screen.
  const onScriptGenerated = useCallback(async (data, choices) => {
    const nextJob = { ...data, voice: choices.voice, resolution: choices.resolution, queue_item_id: choices.queueItemId || '' }
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
          voice: choices.voice || '', music_desc: nextJob.music_desc || '',
          queue_item_id: choices.queueItemId || '',
        })
        if (r.started) go('progress', { workDir: nextJob.work_dir })
        else go('queue')
        return
      } catch {
        // fall through to manual review if enqueue failed
      }
    }
    go('script')
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
      resolution: loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
    })
    go('script')
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
        resolution: item.gen_resolution || loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
        style: item.gen_style || loaded.style || '',
        queue_item_id: item.id,
      })
      go('script')
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
    switch (route) {
      case 'home': return <Home go={go} initialTopic={topic} setTopic={setTopic} />
      case 'create': return <Create seed={createSeed} meta={meta} onGenerated={onScriptGenerated} />
      case 'script': return <Script job={job} setJob={setJob} meta={meta} onGenerate={onGenerationStarted} go={go} />
      case 'progress': return <Progress workDir={progressDir} job={job} go={go} onOpenScript={onOpenScript} />
      case 'remix': return <Remix workDir={progressDir} go={go} />
      case 'queue': return <Queue go={go} onEditScript={onEditQueueScript} meta={meta} />
      case 'youtube': return <YouTube go={go} initial={ytInitial} meta={meta} />
      case 'library': return <Library go={go} onOpenProgress={(wd) => go('progress', { workDir: wd })} onOpenRemix={(wd) => go('remix', { workDir: wd })} onOpenEdit={(wd) => go('editfilm', { workDir: wd })} />
      case 'editfilm': return <EditFilm workDir={editFilmDir} go={go} />
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
