import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import {
  Card, Field, Button, Chip, Check, Icon, Banner, Segmented, RegenLabel, GuidedRegenButton,
  VersionStrip, VideoVersionStrip, MusicVersionStrip, InpaintModal, TrimModal, ContinueModal, voiceMetaMap, voiceLabel, SceneTypeControls, ActedPrompt, isActedMode, hasActedShape, CatalogueRefCard,
} from '../components.jsx'
import { api, fileUrl } from '../api.js'
import PerformanceScenes from './PerformanceScenes.jsx'
import ScriptVisuals from './ScriptVisuals.jsx'

// Quick-instruction presets for the "tell it how" Re-generate popovers.
const REGEN_CHIPS = {
  title: ['Shorter', 'Punchier', 'More literal'],
  narration: ['Shorten', 'Expand', 'Simpler', 'More dramatic'],
  image_prompt: ['More detail', 'Simpler', 'Wider shot'],
  video_prompt: ['More motion', 'Slower pace', 'Static camera'],
  image: ['More detail', 'Brighter', 'Different angle'],
  video: ['More motion', 'Slower pace', 'Different angle'],
  cover: ['Bolder', 'Simpler', 'More dramatic'],
}

const resPixels = (name) => {
  const m = /\((\d+)[×x](\d+)\)/.exec(name || '')
  return m ? Number(m[1]) * Number(m[2]) : 0
}

// Lightbox chrome (matches Script characters / scene enlargements).
const LB_BTN = {
  position: 'absolute', zIndex: 2, border: 'none', color: '#fff',
  background: 'rgba(20,22,24,.55)', backdropFilter: 'blur(6px)',
  width: 46, height: 46, borderRadius: '50%', fontSize: 18,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
}

const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = () => reject(new Error('Could not read that file.'))
  r.readAsDataURL(file)
})

// Mirror app._character_mentions: whole-word name/alias in image prompt + narration.
const characterMentions = (scene, character) => {
  const text = `${scene?.image_prompt || ''} ${scene?.narration || ''}`
  const tokens = [character?.name || '', ...((character?.aliases) || [])]
  return tokens.some((tok) => {
    const t = (tok || '').trim()
    if (!t) return false
    try {
      return new RegExp(`\\b${t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(text)
    } catch {
      return text.toLowerCase().includes(t.toLowerCase())
    }
  })
}

// Poll film-task status until done/error/cancelled.
const waitFilmTask = (taskId) => new Promise((resolve, reject) => {
  const poll = setInterval(async () => {
    try {
      const t = await api.filmTaskStatus(taskId)
      if (t.status === 'done') { clearInterval(poll); resolve(t) }
      else if (t.status === 'error' || t.status === 'cancelled') {
        clearInterval(poll); reject(new Error(t.error || `Task ${t.status}`))
      }
    } catch (e) { clearInterval(poll); reject(e) }
  }, 3000)
})

// ── Per-scene card (Scenes tab) ───────────────────────────────────────────────

function SceneCard({
  scene, index, total, jobId, workDir, resolution, style,
  voices, filmVoice, voiceMeta = {}, castOpts = [], actedSilent = false,
  onDelete, onMove, onSaved, onRerenderStart, onRerenderDone, initialTask,
}) {
  const [editing, setEditing] = useState(false)
  const [lightbox, setLightbox] = useState(false)
  const [title, setTitle] = useState(scene.title || '')
  const [narration, setNarration] = useState(scene.narration || '')
  // Spoken-text split: normally narration IS what the voice reads; the toggle
  // forks a separate spoken line (kept in scene metadata while on).
  const [split, setSplit] = useState(!!(scene.tts_text || '').trim())
  const [ttsText, setTtsText] = useState(scene.tts_text || '')
  const [voice, setVoice] = useState(scene.voice || '')
  const [imagePrompt, setImagePrompt] = useState(scene.image_prompt || '')
  const [videoPrompt, setVideoPrompt] = useState(scene.video_prompt || '')
  // Scene type (narration | dialogue | silent) + dialogue shots + silent
  // duration — mirrors the Script editor via the shared SceneTypeControls.
  const [sceneType, setSceneType] = useState({
    mode: scene.mode || 'narration', lines: scene.lines || [], duration: scene.duration || 0,
    setting: scene.setting || '', camera: scene.camera || '', soundscape: scene.soundscape || '',
    cast: scene.cast || [], beats: scene.beats || [], seconds: scene.seconds || 0,
    singing: !!scene.singing,
  })
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
  const [trim, setTrim] = useState(false)
  const [trimErr, setTrimErr] = useState('')
  const [cont, setCont] = useState(false)
  const [contErr, setContErr] = useState('')
  const pollRef = useRef(null)
  const resumedRef = useRef(null)

  // Load a scene into the editor fields. The card seeds its state from the
  // props once per scene id, so a server-rewritten scene (mode conversion) has
  // to be adopted explicitly — a refetch alone leaves the old values on screen.
  const adopt = (s) => {
    if (!s) return
    setTitle(s.title || '')
    setNarration(s.narration || '')
    setSplit(!!(s.tts_text || '').trim())
    setTtsText(s.tts_text || '')
    setVoice(s.voice || '')
    setImagePrompt(s.image_prompt || '')
    setVideoPrompt(s.video_prompt || '')
    setSceneType({
      mode: s.mode || 'narration', lines: s.lines || [], duration: s.duration || 0,
      setting: s.setting || '', camera: s.camera || '', soundscape: s.soundscape || '',
      cast: s.cast || [], beats: s.beats || [], seconds: s.seconds || 0,
      singing: !!s.singing,
    })
  }

  useEffect(() => {
    adopt(scene)
    setEditing(false)
  }, [scene.id])

  const persist = async (override = null) => {
    const st = override || sceneType
    try {
      await api.saveScene(jobId, scene.id, {
        title, narration, tts_text: split ? ttsText : '', voice, image_prompt: imagePrompt, video_prompt: videoPrompt,
        mode: st.mode || 'narration', lines: st.lines || [], duration: st.duration || 0,
        // Acted-scene fields — the server assembles the video prompt from these.
        setting: st.setting ?? null, camera: st.camera ?? null,
        soundscape: st.soundscape ?? null, cast: st.cast ?? null,
        beats: st.beats ?? null, seconds: st.seconds ?? null,
      })
    } catch (e) {
      setError(e.message)
    }
  }
  // Patch scene-type state; persist the computed value immediately for discrete
  // edits (commit=true) and on blur (commitType) — functional updater reads the
  // latest state so nothing saves a stale value.
  const changeType = (patch, commit) => setSceneType((s) => { const u = { ...s, ...patch }; if (commit) persist(u); return u })
  const commitType = () => setSceneType((s) => { persist(s); return s })

  const setters = { title: setTitle, narration: setNarration, image_prompt: setImagePrompt, video_prompt: setVideoPrompt }
  const regenField = async (field, instruction = '') => {
    setFieldBusy(field); setError('')
    try {
      const r = await api.regenField(jobId, scene.id, field, { title, narration, image_prompt: imagePrompt, video_prompt: videoPrompt, instruction })
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

  const deleteVersion = async (versionId) => {
    setSelecting(true)
    setError('')
    try {
      const r = await api.deleteFilmPreview(workDir, scene.id, versionId)
      setHistory(r.history)
    } catch (e) {
      setError(e.message)
    } finally {
      setSelecting(false)
    }
  }

  const deleteVideoVersion = async (versionId) => {
    setSelecting(true)
    setError('')
    try {
      const r = await api.deleteFilmVideo(workDir, scene.id, versionId)
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

  const applyTrim = async (endSeconds) => {
    setBusy('trim'); setTrimErr('')
    try {
      const r = await api.trimFilmScene(workDir, scene.id, endSeconds)
      setVideoHistory(r.video_history)
      setTrim(false)
    } catch (e) { setTrimErr(e.message) } finally { setBusy('') }
  }

  // The continuation renders like a re-shoot (worker queue, minutes) rather than
  // like a trim, so the modal closes and the card shows the same busy state.
  const applyContinue = async ({ seconds, direction, lines }) => {
    setBusy('continue'); setContErr(''); setError('')
    onRerenderStart('continue')
    try {
      const r = await api.continueFilmScene(workDir, scene.id, { seconds, direction, lines })
      setCont(false)
      if (r.task_id) startPolling(r.task_id)
      else { setBusy(''); onRerenderDone() }
    } catch (e) {
      setBusy(''); setContErr(e.message); onRerenderDone()
    }
  }

  const rerender = async (component, instruction = '') => {
    await persist()
    setBusy(component)
    setError('')
    onRerenderStart(component)
    try {
      const r = await api.rerenderFilmScene(workDir, scene.id, component, instruction)
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

      {trim && videoUrl && (
        <TrimModal src={videoUrl} busy={busy === 'trim'} error={trimErr}
          onApply={applyTrim} onClose={() => setTrim(false)} />
      )}

      {cont && videoUrl && (
        <ContinueModal src={videoUrl} castOpts={sceneType.cast?.length ? sceneType.cast : castOpts}
          busy={busy === 'continue'} error={contErr}
          onApply={applyContinue} onClose={() => setCont(false)} />
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
                <Field label={<RegenLabel busy={fieldBusy === 'title'} onRegen={(instr) => regenField('title', instr)} chips={REGEN_CHIPS.title}>Title</RegenLabel>}>
                  <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
                </Field>
                <SceneTypeControls scene={sceneType} castOpts={castOpts} actedSilent={actedSilent}
                  onChange={changeType} onCommit={commitType}
                  onConvert={async (m) => {
                    setError('')
                    try {
                      const r = await api.convertSceneMode(jobId, scene.id, m)
                      adopt(r?.scene)
                      onSaved()
                    } catch (e) { setError(e.message) }
                  }} />
                {(sceneType.mode || 'narration') === 'narration' && (<>
                  <Field label={<RegenLabel busy={fieldBusy === 'narration'} onRegen={(instr) => regenField('narration', instr)} icon="microphone-lines" chips={REGEN_CHIPS.narration}>Narration</RegenLabel>}>
                    <textarea className="textarea" rows={3} value={narration} onChange={(e) => setNarration(e.target.value)} />
                  </Field>
                  <div>
                    <Check checked={split}
                      onChange={(v) => { setSplit(v); if (v && !ttsText.trim()) setTtsText(narration) }}
                      label="Split spoken text from the narration — the voice reads its own line while captions keep the narration" />
                    {split && (
                      <div className="mt-10">
                        <Field label="Spoken text — what the voice reads on re-render"
                          hint="Respell tricky words (lead pipes → led pipes, lives → livz) and add [pause] or [pause:1.5] for real silence. Untick to speak the narration again.">
                          <textarea className="textarea" rows={2} value={ttsText} placeholder={narration}
                            onChange={(e) => setTtsText(e.target.value)} />
                        </Field>
                      </div>
                    )}
                  </div>
                  <Field label="Narrator voice" hint="Leave on film narrator unless this scene should use a different voice. Re-render narration after changing it.">
                    <select className="select" value={voice} onChange={(e) => setVoice(e.target.value)}>
                      <option value="">Film narrator ({filmVoice || 'Default (F5-TTS)'})</option>
                      {(voices || []).map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMeta)}</option>)}
                    </select>
                  </Field>
                </>)}
                {isActedMode(sceneType.mode) ? (
                  <ActedPrompt prompt={videoPrompt} edited={!!scene.prompt_edited}
                    refs={(sceneType.cast || []).map((n, i) => ({ slot: i + 1, name: n }))}
                    onSave={async (text) => {
                      try {
                        const r = await api.saveScene(jobId, scene.id, {
                          title, narration, image_prompt: '', video_prompt: videoPrompt,
                          mode: sceneType.mode, prompt: text,
                        })
                        if (r?.scene) setVideoPrompt(r.scene.video_prompt || '')
                        onSaved()
                      } catch (e) { setError(e.message) }
                    }}
                    onRebuild={async () => {
                      try {
                        const r = await api.saveScene(jobId, scene.id, {
                          title, narration, image_prompt: '', video_prompt: videoPrompt,
                          mode: sceneType.mode, prompt: '',
                        })
                        if (r?.scene) setVideoPrompt(r.scene.video_prompt || '')
                        onSaved()
                      } catch (e) { setError(e.message) }
                    }} />
                ) : (<>
                <Field label={<RegenLabel busy={fieldBusy === 'image_prompt'} onRegen={(instr) => regenField('image_prompt', instr)} icon="image" chips={REGEN_CHIPS.image_prompt}>Image prompt</RegenLabel>}
                  hint={hasActedShape(sceneType.mode, actedSilent, sceneType.singing)
                    ? 'FLUX — the frame this take opens on' : 'FLUX — static frame'}>
                  <textarea className="textarea" rows={3} value={imagePrompt} onChange={(e) => setImagePrompt(e.target.value)} />
                </Field>
                <Field label={<RegenLabel busy={fieldBusy === 'video_prompt'} onRegen={(instr) => regenField('video_prompt', instr)} icon="film" chips={REGEN_CHIPS.video_prompt}>Video prompt</RegenLabel>}
                  hint={hasActedShape(sceneType.mode, actedSilent, sceneType.singing)
                    ? 'Stands in as the setting while the Setting field above is empty'
                    : 'For the video engine (LTX / MiniMax H3) — motion & camera'}>
                  <textarea className="textarea" rows={3} value={videoPrompt} onChange={(e) => setVideoPrompt(e.target.value)} />
                </Field>
                {/* A performed silent take gets the same assembled H3 prompt a
                    dialogue scene shows — it is shot the same way. */}
                {hasActedShape(sceneType.mode, actedSilent, sceneType.singing) && (
                  <ActedPrompt label="Acted prompt" prompt={scene.acted_prompt || ''} edited={!!scene.prompt_edited}
                    refs={(sceneType.cast || []).map((n, i) => ({ slot: i + 1, name: n }))}
                    onSave={async (text) => {
                      try {
                        await api.saveScene(jobId, scene.id, {
                          title, narration, image_prompt: imagePrompt, video_prompt: videoPrompt,
                          mode: sceneType.mode, prompt: text,
                        })
                        onSaved()
                      } catch (e) { setError(e.message) }
                    }}
                    onRebuild={async () => {
                      try {
                        await api.saveScene(jobId, scene.id, {
                          title, narration, image_prompt: imagePrompt, video_prompt: videoPrompt,
                          mode: sceneType.mode, prompt: '',
                        })
                        onSaved()
                      } catch (e) { setError(e.message) }
                    }} />
                )}
                </>)}
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

              {(sceneType.mode || 'narration') === 'narration' && (
                <Button variant="ghost" icon="microphone-lines" size="sm" disabled={isRendering}
                  onClick={() => rerender('narration')}>
                  {busy === 'narration' ? 'Rendering…' : 'Narration'}
                </Button>
              )}
              {/* An acted scene's frame is a reference, not a render input —
                  Image buttons stay for narrated scenes; acted scenes get a
                  Remove instead (the take then renders reference-only). */}
              {!isActedMode(sceneType.mode) && (<>
              <GuidedRegenButton variant="ghost" icon="image" size="sm" disabled={isRendering}
                label="Image" busyLabel="Rendering…" busy={busy === 'image'}
                onRegen={(instr) => rerender('image', instr)} chips={REGEN_CHIPS.image} align="left" />
              <Button variant="ghost" icon="wand-magic-sparkles" size="sm" disabled={isRendering || !previewUrl}
                onClick={() => { setInpaintErr(''); setInpaint(true) }}>
                Edit image
              </Button>
              </>)}
              {isActedMode(sceneType.mode) && previewUrl && (
                <Button variant="ghost" icon="trash-can" size="sm" disabled={isRendering}
                  title="Delete the first-frame image — the next shoot renders from portraits and visuals only"
                  onClick={async () => {
                    setError('')
                    try { await api.removeScenePreview(jobId, scene.id); onSaved() }
                    catch (e) { setError(e.message) }
                  }}>Remove first frame</Button>
              )}
              <GuidedRegenButton variant="ghost" icon="film" size="sm" disabled={isRendering}
                label={isActedMode(sceneType.mode) ? 'Shoot again' : 'Video'} busyLabel="Rendering…" busy={busy === 'video'}
                onRegen={(instr) => rerender('video', instr)} chips={REGEN_CHIPS.video} align="left" />
              <Button variant="ghost" icon="scissors" size="sm" disabled={isRendering || !videoUrl}
                title="Cut the tail off this scene's clip — the untrimmed take is kept"
                onClick={() => { setTrimErr(''); setTrim(true) }}>
                {busy === 'trim' ? 'Trimming…' : 'Trim'}
              </Button>
              {/* Only offered where it can actually work: an acted take whose
                  motion context is still on a worker and still matches the clip
                  in the cut (scene.can_continue). */}
              {isActedMode(sceneType.mode) && scene.can_continue && (
                <Button variant="ghost" icon="forward-step" size="sm" disabled={isRendering || !videoUrl}
                  title="Shoot a few more seconds carrying on from the last frame — same take, no cut"
                  onClick={() => { setContErr(''); setCont(true) }}>
                  {busy === 'continue' ? 'Shooting…' : 'Continue'}
                </Button>
              )}

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
              onSelect={selectVersion} onDelete={deleteVersion} aspect={aspect} busy={selecting || isRendering} />

            <VideoVersionStrip versions={videoHistory?.versions} selected={videoHistory?.selected}
              onSelect={selectVideoVersion} onDelete={deleteVideoVersion} aspect={aspect} busy={selecting || isRendering} />
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
  const [localizeLangs, setLocalizeLangs] = useState({})
  const [localizeLang, setLocalizeLang] = useState('')
  const [localizeBusy, setLocalizeBusy] = useState(false)
  const [locData, setLocData] = useState(null)      // saved localizations + original language
  const [locEdit, setLocEdit] = useState('')        // lang code open in the narration editor
  const [locDraft, setLocDraft] = useState({})      // {sceneId: edited translated text}
  const [locSaveBusy, setLocSaveBusy] = useState(false)
  const [locAudioBusy, setLocAudioBusy] = useState('')
  const [upscaleResolution, setUpscaleResolution] = useState('')
  const [upscaleMode, setUpscaleMode] = useState('fast')
  const [ffCoverBusy, setFfCoverBusy] = useState(false)
  const [ffSeconds, setFfSeconds] = useState(1)
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

  // Cover image (same store + endpoints as the Script Cover tab / Publish)
  const [coverUrl, setCoverUrl] = useState('')
  const [coverHist, setCoverHist] = useState(null)
  const [coverEdit, setCoverEdit] = useState(false)
  const [coverEditErr, setCoverEditErr] = useState('')
  // Cover phrase: the short text on the cover + the opening burn (per film).
  const [coverPhrase, setCoverPhrase] = useState('')
  const [coverPhraseSaved, setCoverPhraseSaved] = useState('')
  const [coverPhraseDefault, setCoverPhraseDefault] = useState('')
  // Whether the cover has a text-free background (cover typography) — drives
  // "Re-apply title text"; covers predating it need one regeneration first.
  const [coverHasBg, setCoverHasBg] = useState(false)

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

    // Title + description + cover for the finished film (same store as Cover / Publish).
    api.ytPostPrefill(workDir).then((p) => {
      if (p.title) {
        setCoverTitle(p.title)
        onTitleChange?.(p.title)
      }
      if (p.description) setDescription(p.description)
      setCoverUrl(p.cover_url || '')
      setCoverPhrase(p.cover_phrase || '')
      setCoverPhraseSaved(p.cover_phrase || '')
      setCoverPhraseDefault(p.cover_phrase_default || '')
      setCoverHasBg(!!p.cover_has_bg)
      if (p.first_frame_cover_seconds) setFfSeconds(p.first_frame_cover_seconds)
    }).catch(() => {})
    api.coverHistory(workDir).then((r) => setCoverHist(r.history)).catch(() => {})
    api.listLocalizeLanguages().then(setLocalizeLangs).catch(() => {})
    api.localizeScripts(workDir).then(setLocData).catch(() => {})
  }, [workDir])

  const refreshLocalizations = () => api.localizeScripts(workDir).then(setLocData).catch(() => {})

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

  const regenCover = async (instruction = '') => {
    setYtBusy('cover'); setError('')
    let pollTimer = null
    try {
      const { task_id: tid } = await api.ytCover({ work_dir: workDir, title: coverTitle || filmTitle || '', resolution: currentResolution, instruction })
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
      const r = await api.coverSelect(workDir, versionId)
      setCoverUrl(r.cover_url || ''); setCoverHist(r.history)
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const deleteCover = async (versionId) => {
    setYtBusy('cover'); setError('')
    try {
      const r = await api.coverDelete(workDir, versionId)
      setCoverHist(r.history)
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const applyCoverEdit = async (mask, editPrompt, denoise) => {
    setYtBusy('coveredit'); setCoverEditErr('')
    try {
      const r = await api.coverInpaint(workDir, mask, editPrompt, denoise)
      setCoverUrl(r.cover_url || ''); setCoverHist(r.history); setCoverEdit(false)
    } catch (e) { setCoverEditErr(e.message) } finally { setYtBusy('') }
  }

  const savePhrase = async () => {
    setYtBusy('phrase'); setError('')
    try {
      const r = await api.saveCoverPhrase(workDir, coverPhrase)
      setCoverPhrase(r.cover_phrase || '')
      setCoverPhraseSaved(r.cover_phrase || '')
      setCoverPhraseDefault(r.cover_phrase_default || '')
      if (r.cover_url) setCoverUrl(r.cover_url)
      setStatus(r.retexted
        ? 'Saved — the title was re-applied to the cover instantly.'
        : 'Saved the cover phrase — regenerate the cover or burn the opening to use it.')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  // Re-composite the title onto the cover's saved text-free background —
  // applies phrase or Styles-tab typography changes without regenerating art.
  const retextCover = async () => {
    setYtBusy('retext'); setError('')
    try {
      const r = await api.coverRetext(workDir)
      if (r.cover_url) setCoverUrl(r.cover_url)
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
              setStatus(t.total
                ? `Upscaling scenes (${t.current || 0}/${t.total} done)…`
                : 'Upscaling the final video…')
            } else if (t.step === 'finalize') {
              setStatus('Assembling the upscaled film…')
            }
          } catch (e) { clearInterval(poll); reject(e) }
        }, 3000)
      })
    } catch (e) { setError(e.message) } finally { setUpscaleBusy(false) }
  }

  const burnFirstFrameCover = async () => {
    setFfCoverBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.firstFrameCover({
        work_dir: data.work_dir, seconds: ffSeconds,
      })
      await new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
          try {
            const t = await api.filmTaskStatus(task_id)
            if (t.status === 'done') {
              clearInterval(poll)
              if (t.final_url) setData((d) => ({ ...d, final_url: t.final_url }))
              if (t.video_history) setVideoHistory(t.video_history)
              setStatus('Burned the cover into the opening — the previous cut is kept as a version.')
              resolve()
            } else if (t.status === 'error' || t.status === 'cancelled') {
              clearInterval(poll); reject(new Error(t.error || `First-frame cover ${t.status}.`))
            } else {
              setStatus('Burning the cover into the opening…')
            }
          } catch (e) { clearInterval(poll); reject(e) }
        }, 3000)
      })
    } catch (e) { setError(e.message) } finally { setFfCoverBusy(false) }
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

  const deleteVideoVersion = async (versionId) => {
    setUpscaleBusy(true); setError(''); setStatus('')
    try {
      const r = await api.deleteRemixVideo(data.work_dir, versionId)
      if (r.video_history) setVideoHistory(r.video_history)
      setStatus('Deleted the final-video version.')
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

  // Poll a localize-family film task to completion, narrating progress in the
  // status banner; resolves with the task's final payload.
  const pollLocalizeTask = (taskId) => new Promise((resolve, reject) => {
    const poll = setInterval(async () => {
      try {
        const t = await api.filmTaskStatus(taskId)
        if (t.status === 'done') {
          clearInterval(poll)
          if (t.final_url) setData((d) => ({ ...d, final_url: t.final_url }))
          if (t.video_history) setVideoHistory(t.video_history)
          resolve(t)
        } else if (t.status === 'error' || t.status === 'cancelled') {
          clearInterval(poll); reject(new Error(t.error || `Localization ${t.status}.`))
        } else if (t.step === 'translate') {
          setStatus('Translating narration…')
        } else if (t.step === 'narration' && t.fanout) {
          setStatus(`Synthesizing ${t.total} scenes across the TTS workers (${t.current}/${t.total} done)…`)
        } else if (t.step === 'narration' && t.scene_id) {
          setStatus(`Synthesizing scene ${t.scene_id}${t.total ? ` (${t.current}/${t.total})` : ''}…`)
        } else if (t.step === 'finalize') {
          setStatus('Assembling the localized film…')
        }
      } catch (e) { clearInterval(poll); reject(e) }
    }, 3000)
  })

  const localizeFilm = async () => {
    setLocalizeBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.localizeFilm({ work_dir: data.work_dir, language: localizeLang })
      await pollLocalizeTask(task_id)
      setStatus(`Localized the film to ${localizeLangs[localizeLang] || localizeLang}.`)
      await refreshLocalizations()
    } catch (e) { setError(e.message) } finally { setLocalizeBusy(false) }
  }

  const editingLoc = locData?.localizations?.find((l) => l.lang === locEdit)
  const locChanged = editingLoc ? editingLoc.scenes.reduce((acc, s) => {
    const t = (locDraft[s.id] ?? s.translated).trim()
    if (t && t !== s.translated) acc[String(s.id)] = t
    return acc
  }, {}) : {}
  const locChangedCount = Object.keys(locChanged).length

  const saveLocalized = async () => {
    if (!editingLoc || !locChangedCount) return
    setLocSaveBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.saveLocalizeScript({
        work_dir: data.work_dir, language: locEdit, scenes: locChanged,
      })
      await pollLocalizeTask(task_id)
      setStatus(`Updated the ${editingLoc.name} narration.`)
      setLocDraft({})
      await refreshLocalizations()
    } catch (e) { setError(e.message) } finally { setLocSaveBusy(false) }
  }

  const downloadDubbedAudio = async (lang) => {
    setLocAudioBusy(lang); setError('')
    try {
      const { audio_url } = await api.buildLocalizeAudio({ work_dir: data.work_dir, language: lang })
      const a = document.createElement('a')
      a.href = audio_url
      a.download = `${(filmTitle || 'film').replace(/[^\w-]+/g, '_')}_${lang}_audio.m4a`
      document.body.appendChild(a); a.click(); a.remove()
      await refreshLocalizations()
    } catch (e) { setError(e.message) } finally { setLocAudioBusy('') }
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

  const anyBusy = busy || musicBusy || narratorBusy || upscaleBusy || ffCoverBusy || localizeBusy || locSaveBusy || !!locAudioBusy

  // Language of the currently selected final cut, for the marking chip. Only
  // shown once the film has language info (a localization or a tagged version).
  const selVersion = videoHistory?.versions?.find((v) => v.id === videoHistory?.selected)
  const cutLang = selVersion?.lang || locData?.original_lang || ''
  const isOriginalCut = !selVersion?.lang || selVersion?.lang === locData?.original_lang
  const cutLangName = cutLang ? (localizeLangs[cutLang] || cutLang.toUpperCase()) : ''
  const showLangChip = !!cutLang && ((locData?.localizations?.length || 0) > 0 || !!selVersion?.lang)

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
            <span className="row center gap-8">
              <Chip tone="ok" dot>Final cut</Chip>
              {showLangChip && (
                <Chip>
                  <Icon name="language" /> {cutLangName}{isOriginalCut ? ' · original' : ''}
                </Chip>
              )}
            </span>
            <span className="muted mono">{data.work_dir}</span>
          </div>
          {(videoHistory?.versions?.length || 0) > 1 && (
            <div style={{ padding: '0 20px 18px' }}>
              <VideoVersionStrip versions={videoHistory?.versions} selected={videoHistory?.selected}
                onSelect={selectVideoVersion} onDelete={deleteVideoVersion} aspect={aspect} busy={anyBusy}
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

        {/* Cover image (same endpoints as the Script Cover tab / Publish) */}
        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm">Cover image</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>YouTube thumbnail — reused when publishing.</p>
          <div className="mt-16" style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: aspect }}>
            {coverUrl
              ? <img src={coverUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
              : <div className="gfill g2" style={{ position: 'absolute', inset: 0 }}></div>}
          </div>
          <div className="mt-16">
            <Field label="Cover phrase" hint="The short text drawn on the cover — follows the title until you edit it. Wrap a word in *asterisks* to give it the accent colour.">
              <input className="input" value={coverPhrase} maxLength={80} disabled={!!ytBusy}
                onChange={(e) => { setCoverPhrase(e.target.value); setStatus('') }} />
            </Field>
            {coverPhrase !== coverPhraseSaved && (
              <div className="row center gap-8 mt-8">
                <Button variant="primary" icon="floppy-disk" disabled={ytBusy === 'phrase' || !coverPhrase.trim()}
                  onClick={savePhrase}>{ytBusy === 'phrase' ? 'Saving…' : 'Save phrase'}</Button>
                <Button variant="ghost" disabled={ytBusy === 'phrase'}
                  onClick={() => setCoverPhrase(coverPhraseSaved)}>Cancel</Button>
              </div>
            )}
            {coverPhrase === coverPhraseSaved && coverPhraseSaved !== coverPhraseDefault && (
              <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                Custom phrase — <a role="button" tabIndex={0}
                  style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                  onClick={() => setCoverPhrase(coverPhraseDefault)}>use the title again</a>
                {' '}(“{coverPhraseDefault}”), then Save.
              </div>
            )}
          </div>
          <GuidedRegenButton block variant="ghost" icon="rotate-right"
            label={coverUrl ? 'Regenerate cover' : 'Generate cover'} busyLabel="Generating…"
            busy={ytBusy === 'cover'} disabled={!!ytBusy}
            onRegen={regenCover} chips={REGEN_CHIPS.cover} />
          <Button variant="ghost" block icon="wand-magic-sparkles" disabled={!coverUrl || !!ytBusy}
            onClick={() => { setCoverEditErr(''); setCoverEdit(true) }}>Edit cover</Button>
          {coverHasBg && (
            <Button variant="ghost" block icon="font" disabled={!!ytBusy}
              onClick={retextCover}>{ytBusy === 'retext' ? 'Re-applying title…' : 'Re-apply title text'}</Button>
          )}
          <VersionStrip versions={coverHist?.versions} selected={coverHist?.selected}
            onSelect={selectCover} onDelete={deleteCover} aspect={aspect} busy={ytBusy === 'cover' || ytBusy === 'coveredit'} />
          {/* The burn lives with the cover it stamps — Shorts ignore uploaded
              thumbnails and pick their own frame from the video. */}
          <div className="mt-24" style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
            <span className="label-sm">Opening cover</span>
            <p className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
              Shorts ignore thumbnails and pick their own frame — burn the cover into the
              opening of the film so there is one worth picking. Nothing is prepended, so
              the timing never shifts; the previous cut is kept as a version.
            </p>
            <div className="mt-16">
              <Field label="Hold for (seconds)"
                hint="The cover freezes the picture for this long while the audio keeps running. Under ~1s and YouTube’s frame picker tends to skip it.">
                <input className="input" type="number" min={0.04} max={3} step={0.1}
                  value={ffSeconds} disabled={anyBusy}
                  onChange={(e) => setFfSeconds(+e.target.value)} style={{ maxWidth: 120 }} />
              </Field>
            </div>
            <div className="mt-16">
              <Button variant="primary" block icon="image"
                disabled={anyBusy || !coverUrl}
                onClick={burnFirstFrameCover}>
                {ffCoverBusy ? 'Burning…' : 'Burn into the opening'}
              </Button>
            </div>
            {!coverUrl && (
              <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                Generate a cover image first.
              </p>
            )}
          </div>
        </Card>

        {data.can_remix === false && (
          <Card span={4} padLg className="reveal reveal-d2">
            <span className="label-sm">Audio</span>
            <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>
              This film's voices and ambience were generated with the picture, in the
              same pass — there is no separate music or narration track to re-balance,
              re-voice or translate. Re-render a scene to change how it sounds.
            </p>
          </Card>
        )}

        {data.can_remix !== false && (
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
        )}

        {data.can_remix !== false && (
        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="microphone-lines" style={{ color: 'var(--ink-3)', width: 16 }} /> Narrator</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Change the narrator for every scene and rebuild the final audio.</p>
          <div className="mt-24">
            <Field label="Narrator voice">
              <select className="select" value={voice} disabled={anyBusy}
                onChange={(e) => setVoice(e.target.value)}>
                {(data.voices || []).map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMetaMap(meta.config?.voices))}</option>)}
              </select>
            </Field>
          </div>
          <div className="mt-24">
            <Button variant="primary" block icon="microphone-lines" disabled={anyBusy} onClick={regenNarrator}>
              {narratorBusy ? 'Regenerating…' : 'Regenerate narration'}
            </Button>
          </div>
        </Card>
        )}

        {data.can_remix !== false && (
        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="language" style={{ color: 'var(--ink-3)', width: 16 }} /> Localize this film</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>
            Translate the narration and re-speak it in another language — same
            voice, same visuals and music, no re-rendering. Kept as a switchable
            version below. Dialogue and silent scenes keep their original language.
          </p>
          <div className="mt-24">
            <Field label="Target language">
              <select className="select" value={localizeLang} disabled={anyBusy}
                onChange={(e) => setLocalizeLang(e.target.value)}>
                <option value="">Choose a language…</option>
                {Object.entries(localizeLangs).sort((a, b) => a[1].localeCompare(b[1])).map(([code, name]) => (
                  <option key={code} value={code}>{name}</option>
                ))}
              </select>
            </Field>
          </div>
          <div className="mt-24">
            <Button variant="primary" block icon="language" disabled={anyBusy || !localizeLang} onClick={localizeFilm}>
              {localizeBusy ? 'Localizing…' : 'Localize film'}
            </Button>
          </div>
          {(locData?.localizations?.length || 0) > 0 && (
            <div className="mt-24">
              <span className="label-sm">Localizations</span>
              {locData.localizations.map((l) => (
                <div key={l.lang} className="row center between gap-8 mt-8" style={{ flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 13 }}>
                    {l.name} <span className="muted mono" style={{ fontSize: 11 }}>{l.lang.toUpperCase()}</span>
                  </span>
                  <span className="row center gap-8">
                    <Button variant="ghost" icon="pen" disabled={anyBusy}
                      onClick={() => { setLocDraft({}); setLocEdit(locEdit === l.lang ? '' : l.lang) }}>
                      {locEdit === l.lang ? 'Close editor' : 'Edit narration'}
                    </Button>
                    <Button variant="ghost" icon="music" disabled={anyBusy}
                      onClick={() => downloadDubbedAudio(l.lang)}>
                      {locAudioBusy === l.lang ? 'Building…' : 'Dubbed audio'}
                    </Button>
                  </span>
                </div>
              ))}
              <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                Publishing attaches captions in every localized language automatically.
                “Dubbed audio” builds an audio file aligned to the original cut, ready
                for YouTube Studio&apos;s multi-language audio tracks.
              </p>
            </div>
          )}
        </Card>
        )}

        {editingLoc && (
          <Card span={12} padLg className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm row center gap-10">
                <Icon name="pen" style={{ color: 'var(--ink-3)', width: 16 }} />
                Edit {editingLoc.name} narration
              </span>
              <Button variant="ghost" icon="xmark" disabled={locSaveBusy}
                onClick={() => { setLocEdit(''); setLocDraft({}) }}>Close</Button>
            </div>
            <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>
              Fix any translated line, then apply — only the scenes you changed are
              re-voiced, and the {editingLoc.name} film is reassembled as a new version.
            </p>
            <div className="mt-16" style={{ display: 'grid', gap: 14 }}>
              {editingLoc.scenes.map((s) => {
                const val = locDraft[s.id] ?? s.translated
                const dirty = val.trim() && val.trim() !== s.translated
                return (
                  <div key={s.id}>
                    <span className="label-sm">
                      Scene {s.id}{dirty ? <span style={{ color: 'var(--accent)' }}> · edited</span> : null}
                    </span>
                    <p className="muted" style={{ fontSize: 12, margin: '4px 0 6px' }}>{s.original}</p>
                    <textarea className="textarea" rows={2} value={val} disabled={locSaveBusy}
                      onChange={(e) => setLocDraft((d) => ({ ...d, [s.id]: e.target.value }))} />
                  </div>
                )
              })}
            </div>
            <div className="mt-16 row center gap-10">
              <Button variant="primary" icon="microphone" disabled={anyBusy || !locChangedCount}
                onClick={saveLocalized}>
                {locSaveBusy ? 'Re-voicing…'
                  : locChangedCount ? `Apply — re-voice ${locChangedCount} scene${locChangedCount > 1 ? 's' : ''}`
                  : 'No changes yet'}
              </Button>
              {locChangedCount > 0 && !locSaveBusy && (
                <Button variant="ghost" onClick={() => setLocDraft({})}>Discard edits</Button>
              )}
            </div>
          </Card>
        )}

        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="up-right-and-down-left-from-center" style={{ color: 'var(--ink-3)', width: 16 }} /> Upscale video</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>
            Upscale the finished film and keep it as a selectable final version.
            <strong> Fast</strong> is plain ffmpeg.
            <strong> LTX latent</strong> is the simple model upscaler (latent 2×).
            <strong> LTX IC-LoRA</strong> is the generative{' '}
            <a href="https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler" target="_blank" rel="noreferrer">Pixel Spatial Upscaler</a>.
          </p>
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
                <option value="fast">Fast (ffmpeg)</option>
                <option value="ltx_latent">LTX latent (simple model)</option>
                <option value="ic_lora">LTX IC-LoRA (generative)</option>
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

        <Card span={8} padLg className="reveal reveal-d2">
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

        {coverEdit && (
          <InpaintModal src={coverUrl} aspect={aspect} busy={ytBusy === 'coveredit'} error={coverEditErr}
            onApply={applyCoverEdit} onClose={() => setCoverEdit(false)} />
        )}
      </div>
    </div>
  )
}

// ── Characters tab ────────────────────────────────────────────────────────────

function CharactersTab({ workDir, onSwitchToScenes, reloadKey = 0 }) {
  const [jobId, setJobId] = useState('')
  const [scenes, setScenes] = useState([])
  const [characters, setCharacters] = useState([])
  const [castCatalogue, setCastCatalogue] = useState([])   // style catalogue, read-only
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [charBusy, setCharBusy] = useState('')
  const [charMsg, setCharMsg] = useState('')
  const [aliasDraft, setAliasDraft] = useState({})
  const [charLightbox, setCharLightbox] = useState(null)
  const [redoBusy, setRedoBusy] = useState('')  // character id currently redoing scenes
  const [confirmRedo, setConfirmRedo] = useState('')  // character id pending confirm

  const load = useCallback(async () => {
    setError('')
    try {
      const r = await api.filmScenes(workDir)
      setJobId(r.job_id || '')
      setScenes(r.scenes || [])
      if (r.job_id) {
        const c = await api.scriptCharacters(r.job_id)
        setCharacters(c.characters || [])
        setCastCatalogue(c.catalogue || [])
      } else {
        setCharacters([])
        setCastCatalogue([])
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setLoaded(true)
    }
  }, [workDir])

  useEffect(() => { load() }, [load, reloadKey])

  // Background look generation may still be finishing — poll briefly while any
  // character is missing its image (same pattern as Script Characters).
  useEffect(() => {
    if (!jobId || charBusy || redoBusy) return
    if (!characters.some((c) => !c.has_image)) return
    let alive = true
    let tries = 0
    let timer = null
    const poll = async () => {
      try {
        const r = await api.scriptCharacters(jobId)
        if (!alive) return
        setCharacters(r.characters || [])
        if ((r.characters || []).some((c) => !c.has_image) && tries++ < 12) timer = setTimeout(poll, 4000)
      } catch { /* best-effort */ }
    }
    timer = setTimeout(poll, 4000)
    return () => { alive = false; clearTimeout(timer) }
  }, [jobId, characters, charBusy, redoBusy])

  const charOp = async (id, run) => {
    setCharBusy(id); setError(''); setCharMsg('')
    try {
      const r = await run()
      setCharacters(r.characters || [])
    } catch (e) { setError(e.message) } finally { setCharBusy('') }
  }
  const setCharField = (id, key, val) =>
    setCharacters((arr) => arr.map((c) => (c.id === id ? { ...c, [key]: val } : c)))
  const addCharacter = () => charOp('add', () =>
    api.addScriptCharacter(jobId, { name: '', aliases: [], description: '' }))
  const removeCharacter = (c) => charOp(c.id, () => api.deleteScriptCharacter(jobId, c.id))
  const genCharLook = (c) => charOp(c.id, () => api.generateScriptCharacterPortrait(jobId, c.id, ''))
  const clearCharLook = (c) => charOp(c.id, () => api.clearScriptCharacterImage(jobId, c.id))
  const uploadCharLook = (c, file) => file && charOp(c.id, async () =>
    api.setScriptCharacterImage(jobId, c.id, file.name, await fileToDataUrl(file)))
  const promoteCharacter = (c) => charOp(c.id, async () => {
    const r = await api.promoteScriptCharacter(jobId, c.id)
    setCharMsg(`Saved “${c.name || 'character'}” to your character catalogue.`)
    return r
  })
  const selectCharVersion = (c, versionId) =>
    charOp(c.id, () => api.selectScriptCharacterImage(jobId, c.id, versionId))
  const deleteCharVersion = (c, versionId) =>
    charOp(c.id, () => api.deleteScriptCharacterImage(jobId, c.id, versionId))

  const scenesForChar = (c) => scenes.filter((s) => characterMentions(s, c))

  // Re-render image then video for every scene that features this character, so
  // the new look lands in first frames and the motion clips that use them.
  const redoScenes = async (c) => {
    const matching = scenesForChar(c)
    setConfirmRedo('')
    if (!matching.length) {
      setCharMsg(`No scenes mention “${c.name || 'this character'}” by name or alias.`)
      return
    }
    setRedoBusy(c.id); setError(''); setCharMsg('')
    try {
      setCharMsg(`Re-rendering images for ${matching.length} scene${matching.length === 1 ? '' : 's'}…`)
      const imageWaits = []
      for (const s of matching) {
        const r = await api.rerenderFilmScene(workDir, s.id, 'image')
        if (r.task_id) imageWaits.push(waitFilmTask(r.task_id))
      }
      await Promise.all(imageWaits)

      setCharMsg(`Re-rendering video for ${matching.length} scene${matching.length === 1 ? '' : 's'}…`)
      const videoWaits = []
      for (const s of matching) {
        const r = await api.rerenderFilmScene(workDir, s.id, 'video')
        if (r.task_id) videoWaits.push(waitFilmTask(r.task_id))
      }
      await Promise.all(videoWaits)

      setCharMsg(
        `Updated ${matching.length} scene${matching.length === 1 ? '' : 's'} with “${c.name || 'character'}”. `
        + 'Open Scenes to review, then Reassemble film to refresh the final cut.',
      )
    } catch (e) {
      setError(e.message)
    } finally {
      setRedoBusy('')
    }
  }

  // Character look lightbox
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
  const aliasValue = (c) => (aliasDraft[c.id] ?? (c.aliases || []).join(', '))
  const commitAliases = (c) => {
    const raw = aliasDraft[c.id]
    setAliasDraft((d) => { const n = { ...d }; delete n[c.id]; return n })
    if (raw === undefined) return
    const aliases = raw.split(',').map((s) => s.trim()).filter(Boolean)
    setCharField(c.id, 'aliases', aliases)
    charOp(c.id, () => api.updateScriptCharacter(jobId, c.id, {
      name: c.name || '', aliases, description: c.description || '',
    }))
  }

  if (!loaded) return <p className="muted">Loading characters…</p>
  if (!jobId) {
    return (
      <Card span={12} well>
        <p className="muted" style={{ margin: 0 }}>Could not resolve this film’s job id — characters can’t be loaded.</p>
      </Card>
    )
  }

  return (
    <div>
      <Banner tone="danger">{error}</Banner>
      {charMsg && <Banner tone="ok">{charMsg}</Banner>}
      {redoBusy && (
        <Banner tone="info">
          <Icon name="spinner" spin /> Updating scenes for this character’s look… This can take a while.
        </Banner>
      )}

      <div className="bento">
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center gap-10">
            <Icon name="user-group" style={{ color: 'var(--ink-3)' }} />
            <span className="muted" style={{ fontSize: 12.5 }}>
              Edit this film’s cast and looks. After changing a look, use <strong>Redo scenes</strong> so
              image and video picks up the new appearance — then reassemble from the Scenes tab.
            </span>
          </div>
        </Card>

        {characters.length === 0 && (
          <Card span={12} well className="reveal reveal-d2">
            <p className="muted" style={{ fontSize: 13, margin: 0 }}>
              No recurring characters for this film. Click <strong>Add character</strong> to define one.
            </p>
          </Card>
        )}

        {characters.map((c, i) => {
          const b = charBusy === c.id || redoBusy === c.id
          const used = scenesForChar(c)
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
                    <Button variant="ghost" size="sm" block icon="rotate-right" disabled={b} onClick={() => genCharLook(c)}>
                      {charBusy === c.id ? 'Painting…' : c.has_image ? 'Regenerate look' : 'Generate look'}
                    </Button>
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
                      onBlur={(e) => {
                        const name = e.target.value
                        setCharField(c.id, 'name', name)
                        charOp(c.id, () => api.updateScriptCharacter(jobId, c.id, {
                          name, aliases: c.aliases || [], description: c.description || '',
                        }))
                      }} />
                  </Field>
                  <Field label="Also called" hint="Comma-separated aliases the narration may use.">
                    <input className="input" value={aliasValue(c)}
                      onChange={(e) => setAliasDraft((d) => ({ ...d, [c.id]: e.target.value }))}
                      onBlur={() => commitAliases(c)} />
                  </Field>
                  <Field label="Appearance" hint="Fixed look — drawn the same way in every scene.">
                    <textarea className="textarea" rows={4} value={c.description || ''}
                      onChange={(e) => setCharField(c.id, 'description', e.target.value)}
                      onBlur={(e) => {
                        const description = e.target.value
                        setCharField(c.id, 'description', description)
                        charOp(c.id, () => api.updateScriptCharacter(jobId, c.id, {
                          name: c.name || '', aliases: c.aliases || [], description,
                        }))
                      }} />
                  </Field>
                  <div className="row center gap-8 row--wrap">
                    <Chip tone={used.length ? 'ok' : 'neutral'}>
                      {used.length ? `In ${used.length} scene${used.length === 1 ? '' : 's'}` : 'Not in any scene'}
                    </Chip>
                  </div>
                  <div className="row gap-10 row--wrap">
                    {confirmRedo === c.id ? (
                      <>
                        <Button variant="primary" icon="rotate-right" disabled={b || !used.length}
                          onClick={() => redoScenes(c)}>
                          {redoBusy === c.id ? 'Re-rendering…' : `Confirm redo ${used.length}`}
                        </Button>
                        <Button variant="ghost" disabled={b} onClick={() => setConfirmRedo('')}>Cancel</Button>
                      </>
                    ) : (
                      <Button variant="primary" icon="rotate-right" disabled={b || !used.length || !!redoBusy}
                        onClick={() => setConfirmRedo(c.id)}>
                        Redo scenes
                      </Button>
                    )}
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

        {/* Catalogue members appear at the same level — the film uses them,
            but they are shared across films, so they edit in Settings. */}
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

            <div onClick={(e) => e.stopPropagation()}
              style={{ position: 'absolute', top: 18, left: 22, color: 'rgba(255,255,255,.92)', fontSize: 13, fontWeight: 600, display: 'flex', gap: 10, cursor: 'default' }}>
              <span>{clbChar?.name || 'Character'}</span>
              {clbVersions.length > 1 && <span style={{ opacity: 0.65 }}>· Look {charLightbox.ver + 1} / {clbVersions.length}</span>}
            </div>

            <button type="button" title="Close (Esc)" onClick={(e) => { e.stopPropagation(); setCharLightbox(null) }}
              style={{ ...LB_BTN, top: 14, right: 16, width: 40, height: 40, fontSize: 16, cursor: 'pointer' }}>
              <Icon name="xmark" />
            </button>

            {clbVersions.length > 1 && (
              <>
                <button type="button" title="Previous look (←)" disabled={charLightbox.ver <= 0}
                  onClick={(e) => { e.stopPropagation(); clbVerMove(-1) }}
                  style={{ ...LB_BTN, left: 18, top: '50%', transform: 'translateY(-50%)', cursor: 'pointer', opacity: charLightbox.ver <= 0 ? 0.35 : 1 }}>
                  <Icon name="chevron-left" />
                </button>
                <button type="button" title="Next look (→)" disabled={charLightbox.ver >= clbVersions.length - 1}
                  onClick={(e) => { e.stopPropagation(); clbVerMove(1) }}
                  style={{ ...LB_BTN, right: 18, top: '50%', transform: 'translateY(-50%)', cursor: 'pointer', opacity: charLightbox.ver >= clbVersions.length - 1 ? 0.35 : 1 }}>
                  <Icon name="chevron-right" />
                </button>
              </>
            )}
          </div>
        )}
      </div>

      {charMsg && !redoBusy && onSwitchToScenes && characters.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <Button variant="ghost" icon="film" onClick={onSwitchToScenes}>Open Scenes</Button>
        </div>
      )}
    </div>
  )
}

// ── Scenes tab ────────────────────────────────────────────────────────────────

function ScenesTab({ workDir, meta = {}, onTitle, onSwitchToFilm }) {
  const voiceMeta = voiceMetaMap(meta.config?.voices)
  const [scenes, setScenes] = useState([])
  // Dialogue speaker options: the catalogue characters plus any speakers already
  // used across this film's scenes (so existing casts stay selectable).
  const castOpts = useMemo(() => {
    const names = new Set((meta.config?.characters || []).map((c) => c?.name).filter(Boolean))
    for (const s of scenes) for (const ln of (s.lines || [])) if (ln?.speaker) names.add(ln.speaker)
    return [...names]
  }, [scenes, meta.config?.characters])
  const [jobId, setJobId] = useState('')
  const [resolution, setResolution] = useState('')
  const [style, setStyle] = useState('')
  // The film's style performs its silent scenes on H3 (h3_silent_scenes): those
  // scenes are then written through the acted fields, same as the dialogue ones.
  const [actedSilent, setActedSilent] = useState(false)
  const [voices, setVoices] = useState([])
  const [filmVoice, setFilmVoice] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [assembling, setAssembling] = useState(false)
  const [assembleResult, setAssembleResult] = useState(null)
  const [activeRenders, setActiveRenders] = useState(0)
  const [resumeTasks, setResumeTasks] = useState({})
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    setError('')
    try {
      const r = await api.filmScenes(workDir)
      setScenes(r.scenes || [])
      setJobId(r.job_id || '')
      onTitle?.(r.title || '')
      setResolution(r.resolution || '')
      setStyle(r.style || '')
      setActedSilent(!!r.acted_silent)
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

  // activeRenders is client-side bookkeeping (mount snapshot + per-card
  // callbacks). A running task whose card never resumes a poller — two tasks
  // queued on one scene, or a deleted scene's task — would leave the count
  // stuck above zero and the Reassemble button silently disabled forever.
  // While the count claims renders are active, re-sync with the server so the
  // button always frees up once the work is actually done.
  useEffect(() => {
    if (activeRenders <= 0) return undefined
    const poll = setInterval(() => {
      api.filmTasksForWorkDir(workDir).then((r) => {
        setActiveRenders((r.tasks || []).filter((t) => t.status === 'running').length)
      }).catch(() => {})
    }, 5000)
    return () => clearInterval(poll)
  }, [activeRenders > 0, workDir])

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

  // Append a blank scene (issue #193): write its narration/prompts on the new
  // card, then build it with the re-render buttons (narration → image → video)
  // and reassemble. Position it with the card's move chevrons.
  const addScene = async () => {
    setAdding(true)
    setError('')
    try {
      await api.addFilmScene(workDir)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setAdding(false)
    }
  }

  if (!loaded) return <p className="muted">Loading scenes…</p>

  return (
    <div>
      <div className="row gap-10 row--wrap" style={{ marginBottom: 16 }}>
        <Button variant="primary" icon="circle-nodes" disabled={assembling || activeRenders > 0 || scenes.length === 0} onClick={reassemble}>
          {assembling ? 'Assembling…' : 'Reassemble film'}
        </Button>
        <Button variant="ghost" icon="plus" disabled={adding || assembling} onClick={addScene}>
          {adding ? 'Adding…' : 'Add scene'}
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
            {assembleResult.note && (
              <div style={{ fontSize: 12.5, marginTop: 6, color: 'var(--warn)' }}>
                <Icon name="triangle-exclamation" /> {assembleResult.note}
              </div>
            )}
          </div>
        </div>
      )}

      {scenes.length === 0 ? (
        <Card span={12} well>
          <p className="muted" style={{ margin: 0 }}>No scenes found for this film. “Add scene” starts a new one from scratch.</p>
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
              voiceMeta={voiceMeta}
              castOpts={castOpts}
              actedSilent={actedSilent}
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

const EDIT_TABS = new Set(['film', 'characters', 'scenes', 'performance'])

export default function EditFilm({ workDir, go, meta = {}, initialTab = 'film' }) {
  const [tab, setTab] = useState(EDIT_TABS.has(initialTab) ? initialTab : 'film')
  const [filmTitle, setFilmTitle] = useState('')
  // Any scene that renders as an H3 take — the acted ones plus, when the style
  // performs them, the silent ones. Those takes are conditioned on reference
  // images and shown in the acted view, so this is what decides whether the
  // film gets the visuals wall and the Acted scenes tab.
  const [hasRefTakes, setHasRefTakes] = useState(false)
  const [filmScenes, setFilmScenes] = useState([])    // for the visuals card's scene scoping
  const [filmJobId, setFilmJobId] = useState('')
  const [charReload, setCharReload] = useState(0)     // bumped when the bar adds a character
  // Voice library for the acted view's per-character voice picker. Without it
  // the select has only its "invent" option — and an HTML select whose value
  // isn't among its options silently shows the first one, reading as "no
  // voice" for a character that HAS one.
  const [voiceOpts, setVoiceOpts] = useState([])
  const [voiceMeta, setVoiceMeta] = useState({})
  useEffect(() => {
    api.getConfig().then((c) => {
      const cfg = c?.config || c || {}
      setVoiceOpts((cfg.voices || []).map((v) => v?.name).filter(Boolean))
      setVoiceMeta(voiceMetaMap(cfg.voices))
    }).catch(() => {})
  }, [])

  // Prefill page title from film scenes (lightweight enough for the head), and
  // note whether anything here is shot on the reference engine: the Scenes list
  // keeps every scene whatever the mix, and the acted view is added beside it
  // for the takes — spoken or performed silent — that H3 shoots.
  useEffect(() => {
    if (!workDir) return
    api.filmScenes(workDir).then((r) => {
      if (r.title) setFilmTitle(r.title)
      const list = r.scenes || []
      setHasRefTakes(list.some((s) => hasActedShape(s.mode || s.metadata?.mode, r.acted_silent,
        s.singing ?? s.metadata?.singing)))
      setFilmScenes(list)
      setFilmJobId(r.job_id || '')
    }).catch(() => {})
  }, [workDir])

  useEffect(() => {
    setTab(EDIT_TABS.has(initialTab) ? initialTab : 'film')
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
          { value: 'characters', label: hasRefTakes ? 'Characters & artifacts' : 'Characters' },
          // ONE look whatever the mix: the Scenes editor (every scene, every
          // mode, shiftable between them) plus, when anything is acted, the
          // Acted scenes view — cast slots, portraits, voices, takes.
          { value: 'scenes', label: 'Scenes' },
          ...(hasRefTakes ? [{ value: 'performance', label: 'Acted scenes' }] : []),
        ]} />
      </div>

      {tab === 'performance' && (
        <PerformanceScenes workDir={workDir} voiceOpts={voiceOpts} voiceMeta={voiceMeta} />
      )}
      {tab === 'film' && (
        <FilmTab
          workDir={workDir}
          go={go}
          meta={meta}
          filmTitle={filmTitle}
          onTitleChange={setFilmTitle}
        />
      )}
      {tab === 'characters' && (
        <>
          {/* ONE bar for every reference — characters and things together —
              leading the wall, same as the Script screen. */}
          {hasRefTakes ? (
            <div className="bento" style={{ marginBottom: 20 }}>
              <ScriptVisuals jobId={filmJobId}
                onAddCharacter={filmJobId ? async () => {
                  await api.addScriptCharacter(filmJobId, { name: '', aliases: [], description: '' })
                  setCharReload((k) => k + 1)
                } : undefined}
                sceneIds={filmScenes.map((s) => s.id)}
                castNames={[...new Set([...filmScenes.flatMap((s) => s.cast || []),
                  ...filmScenes.flatMap((s) => (s.lines || []).map((l) => l.speaker))].filter(Boolean))]}
                settingHint={filmScenes.map((s) => s.setting || s.metadata?.setting).find(Boolean) || ''} />
            </div>
          ) : (
            <div className="bento" style={{ marginBottom: 20 }}>
              <Card span={12} well>
                <div className="row between center">
                  <span className="muted" style={{ fontSize: 13 }}>The people this film keeps consistent.</span>
                  <Button variant="primary" size="sm" icon="user-plus" disabled={!filmJobId}
                    onClick={async () => {
                      await api.addScriptCharacter(filmJobId, { name: '', aliases: [], description: '' })
                      setCharReload((k) => k + 1)
                    }}>Add character</Button>
                </div>
              </Card>
            </div>
          )}
          <CharactersTab
            workDir={workDir}
            reloadKey={charReload}
            onSwitchToScenes={() => setTab('scenes')}
          />
        </>
      )}
      {tab === 'scenes' && (
        <ScenesTab
          workDir={workDir}
          meta={meta}
          onTitle={setFilmTitle}
          onSwitchToFilm={() => setTab('film')}
        />
      )}
    </div>
  )
}
