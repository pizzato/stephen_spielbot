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
import Settings from './screens/Settings.jsx'

export default function App() {
  const [route, setRoute] = useState('home')
  const [topic, setTopic] = useState('')          // hero quick-start text
  const [createSeed, setCreateSeed] = useState(null)  // {title, description, scenes} prefill for Create
  const [job, setJob] = useState(null)            // {job_id, work_dir, title, style, music_desc, scenes, voice, resolution}
  const [progressDir, setProgressDir] = useState('')
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
    if (id === 'create') setCreateSeed({
      title: payload?.title ?? payload?.topic ?? '',
      description: payload?.description ?? '',
      scenes: payload?.scenes ?? null,
    })
    // Deep-link into the YouTube tab's Publish view (e.g. from a Films card).
    if (id === 'youtube') setYtInitial(payload?.publishWorkDir ? { view: 'publish', workDir: payload.publishWorkDir } : null)
    setRoute(id)
    window.scrollTo({ top: 0 })
    // Visiting Films marks the new ones as seen (mailbox-style clear).
    if (id === 'library') api.markSeen('films').then(refreshBadges).catch(() => {})
  }, [refreshBadges])

  // Create → Script: a fresh script was generated. If the user opted to
  // auto-approve, launch generation immediately and jump to the render screen.
  const onScriptGenerated = useCallback(async (data, choices) => {
    const nextJob = { ...data, voice: choices.voice, resolution: choices.resolution }
    setJob(nextJob)
    if (choices.autoApprove) {
      try {
        const r = await api.queueFromJob({
          job_id: nextJob.job_id, work_dir: nextJob.work_dir,
          video_title: nextJob.video_title || nextJob.title || '',
          n_scenes: nextJob.scenes?.length || 0,
          style: nextJob.style || '', resolution: choices.resolution || '',
          voice: choices.voice || '', music_desc: nextJob.music_desc || '',
        })
        if (r.started) { setProgressDir(nextJob.work_dir); go('progress') }
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
    setProgressDir(workDir)
    go('progress')
  }, [go])

  const screen = (() => {
    switch (route) {
      case 'home': return <Home go={go} initialTopic={topic} setTopic={setTopic} />
      case 'create': return <Create seed={createSeed} meta={meta} onGenerated={onScriptGenerated} />
      case 'script': return <Script job={job} setJob={setJob} meta={meta} onGenerate={onGenerationStarted} go={go} />
      case 'progress': return <Progress workDir={progressDir} job={job} go={go} />
      case 'remix': return <Remix workDir={progressDir} go={go} />
      case 'queue': return <Queue go={go} />
      case 'youtube': return <YouTube go={go} initial={ytInitial} />
      case 'library': return <Library go={go} onOpenRemix={(wd) => { setProgressDir(wd); go('remix') }} />
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
