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
  const [job, setJob] = useState(null)            // {job_id, work_dir, title, style, music_desc, scenes, voice, resolution}
  const [progressDir, setProgressDir] = useState('')
  const [meta, setMeta] = useState({ config: {}, voices: [], resolutions: [], default_resolution: '' })

  useEffect(() => {
    api.getConfig().then(setMeta).catch(() => {})
  }, [])

  const go = useCallback((id, payload) => {
    if (payload?.topic != null) setTopic(payload.topic)
    setRoute(id)
    window.scrollTo({ top: 0 })
  }, [])

  // Create → Script: a fresh script was generated. If the user opted to
  // auto-approve, launch generation immediately and jump to the render screen.
  const onScriptGenerated = useCallback(async (data, choices) => {
    const nextJob = { ...data, voice: choices.voice, resolution: choices.resolution }
    setJob(nextJob)
    if (choices.autoApprove) {
      try {
        await api.startGeneration({
          job_id: nextJob.job_id, work_dir: nextJob.work_dir,
          video_title: nextJob.video_title || '', title: nextJob.title || '',
          n_scenes: nextJob.scenes?.length || 0, voice: choices.voice || '',
          resolution: choices.resolution || '', music_desc: nextJob.music_desc || '',
          style: nextJob.style || '',
        })
        setProgressDir(nextJob.work_dir)
        go('progress')
        return
      } catch {
        // fall through to manual review if the launch failed
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
      case 'create': return <Create initialTopic={topic} meta={meta} onGenerated={onScriptGenerated} />
      case 'script': return <Script job={job} setJob={setJob} meta={meta} onGenerate={onGenerationStarted} go={go} />
      case 'progress': return <Progress workDir={progressDir} job={job} go={go} />
      case 'remix': return <Remix workDir={progressDir} go={go} />
      case 'queue': return <Queue go={go} />
      case 'youtube': return <YouTube go={go} />
      case 'library': return <Library go={go} onOpenRemix={(wd) => { setProgressDir(wd); go('remix') }} />
      case 'settings': return <Settings meta={meta} setMeta={setMeta} />
      default: return <Home go={go} />
    }
  })()

  return (
    <div className="shell">
      <Sidebar route={route} go={go} />
      <main className="main" key={route}>{screen}</main>
    </div>
  )
}
