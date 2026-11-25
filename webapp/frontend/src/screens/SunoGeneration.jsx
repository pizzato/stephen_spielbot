import React, { useEffect, useId, useRef, useState } from 'react'
import { api } from '../api.js'
import { Button } from '../components.jsx'
import SunoConnection from './SunoConnection.jsx'

const VARIETY = ['Off', 'Normal', 'High', 'Extra', 'Max']
const SOUND_LIMIT = 1000

function checkAccount() {
  return api.sunoAuthStatus().catch((e) => ({ connected: false, error: e.message }))
}

function sunoSound(caption) {
  const sound = caption.trim()
  if (sound.length <= SOUND_LIMIT) return sound

  const summary = sound.match(/^(?:#{1,6}[ \t]+)?Global Metadata:?[ \t]*\r?\n([^\r\n]+)/i)?.[1].trim()
  if (summary && summary.length <= SOUND_LIMIT) return summary

  const prefix = (summary || sound).slice(0, SOUND_LIMIT)
  return (prefix.replace(/\s+\S*$/, '') || prefix).replace(/[\uD800-\uDBFF]$/, '').trimEnd()
}

function Choice({ label, value, options, onChange, disabled }) {
  return (
    <div className="field" role="group" aria-label={label}>
      <span className="field__label">{label}</span>
      <div className="seg seg--wrap suno-settings__choices">
        {options.map(([key, text]) => (
          <button type="button" key={key} className={`seg__opt${value === key ? ' is-active' : ''}`}
            disabled={disabled} aria-pressed={value === key} onClick={() => onChange(key)}>{text}</button>
        ))}
      </div>
    </div>
  )
}

function Slider({ label, value, onChange, disabled, choices }) {
  const display = choices ? choices[value] : `${value}%`
  return (
    <label className="suno-settings__row">
      <span>{label}</span>
      <input type="range" min={0} max={choices ? choices.length - 1 : 100}
        value={value} aria-label={label} aria-valuetext={display} disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))} />
      <output>{display}</output>
    </label>
  )
}

function SunoDialog({ defaults, caption, lyrics, previous, onClose, onGenerate }) {
  const ref = useRef(null)
  const bodyRef = useRef(null)
  const headingId = useId()
  const suggestedDuration = Math.round(defaults.duration)
  const shortenedSound = caption.trim().length > SOUND_LIMIT
  const [form, setForm] = useState(() => previous || ({
    ...defaults, caption: sunoSound(caption), lyrics,
    durationMode: suggestedDuration >= 15 && suggestedDuration <= 360 ? 'custom' : 'auto',
    duration: suggestedDuration >= 15 && suggestedDuration <= 360 ? suggestedDuration : 60,
    max_mode: false, vocal_gender: '', negative_style: '',
    weirdness: 50, style_influence: 50, audio_influence: 50, variety: 0,
  }))
  const [account, setAccount] = useState(null)
  const [voiceList, setVoiceList] = useState(null)
  const [checking, setChecking] = useState(false)
  const connected = !!account?.connected
  const hasVocals = !!lyrics.trim()
  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }))
  const change = (key) => (e) => set(key, e.target.value)

  useEffect(() => {
    const dialog = ref.current
    dialog.showModal()
    let active = true
    checkAccount().then((result) => { if (active) setAccount(result) })
    return () => { active = false; dialog.close() }
  }, [])

  useEffect(() => {
    setVoiceList(null)
    if (!connected || !hasVocals) return
    let active = true
    async function loadVoices() {
      const result = await api.sunoVoices().catch((e) => ({ available: false, voices: [], error: e.message }))
      if (!active) return
      if (result.available === false) {
        const current = await checkAccount()
        if (!active) return
        if (!current.connected) setAccount(current)
      }
      setVoiceList(result)
    }
    loadVoices()
    return () => { active = false }
  }, [connected, hasVocals])

  const models = account?.allowed_models || []
  const voices = voiceList?.voices || []
  const unknownModel = form.model && !models.includes(form.model)
  const unknownVoice = form.voice_id && !voices.some((voice) => voice.id === form.voice_id)
  const advanced = !!account?.advanced_controls
  const custom = form.durationMode === 'custom'
  const validDuration = !custom || (Number.isInteger(Number(form.duration)) && form.duration >= 15 && form.duration <= 360)
  const validText = form.title.trim() && form.title.length <= 256 && form.caption.length <= SOUND_LIMIT
    && (!hasVocals || (form.lyrics.trim() && form.lyrics.length <= 5000))
  const canGenerate = account?.connected && !unknownModel && (!hasVocals || (voiceList && !unknownVoice))
    && validDuration && validText

  const submit = async (e) => {
    e.preventDefault()
    if (!canGenerate || checking) return
    setChecking(true)
    const current = await checkAccount()
    if (!ref.current?.open) return
    setChecking(false)
    setAccount(current)
    if (!current.connected || (form.model && !current.allowed_models?.includes(form.model))
        || advanced !== !!current.advanced_controls) {
      bodyRef.current.scrollTop = 0
      return
    }
    const controls = { variety: form.variety }
    if (advanced) {
      controls.weirdness = form.weirdness / 100
      controls.style_influence = form.style_influence / 100
      if (hasVocals && form.voice_id) {
        controls.audio_influence = form.audio_influence / 100
      }
    }
    onGenerate({
      title: form.title.trim(), caption: form.caption.trim(), lyrics: form.lyrics.trim(),
      model: form.model, voice_id: hasVocals ? form.voice_id : '',
      duration: custom ? Number(form.duration) : null, max_mode: form.max_mode,
      vocal_gender: advanced && hasVocals && !form.voice_id ? form.vocal_gender || null : null,
      negative_style: advanced ? form.negative_style.trim() : '', controls,
    }, form)
  }

  return (
    <dialog ref={ref} className="suno-settings" aria-labelledby={headingId}
      onCancel={(e) => { e.preventDefault(); onClose() }}>
      <form onSubmit={submit}>
        <header className="suno-settings__header">
          <h2 id={headingId}>Generate on Suno</h2>
          {account?.connected && <span className="muted">{account.plan}</span>}
        </header>
        <div ref={bodyRef} className="suno-settings__body stack gap-12" inert={checking ? '' : undefined}>
          {!account && <div className="field__hint" role="status">Checking Suno account…</div>}
          {account && !connected && <SunoConnection account={account} onChange={setAccount} inDialog />}

          <label className="field">
            <span className="field__label">Title</span>
            <input className="input" value={form.title} onChange={change('title')} maxLength={256} required />
          </label>
          <div className="suno-settings__pair">
            <label className="field">
              <span className="field__label">Model</span>
              <select className="select" value={form.model} onChange={change('model')} disabled={!account?.connected}>
                <option value="">Account default</option>
                {unknownModel && <option value={form.model}>{form.model} (saved)</option>}
                {models.map((model) => <option key={model}>{model}</option>)}
              </select>
              {account?.connected && unknownModel && <span className="field__hint">Choose a model available on this plan.</span>}
            </label>
            {hasVocals && <label className="field">
              <span className="field__label">Suno singing voice</span>
              <select className="select" value={form.voice_id} onChange={change('voice_id')} disabled={!connected || !voiceList}>
                <option value="">Model’s own vocalist</option>
                {unknownVoice && <option value={form.voice_id}>{form.voice_id} (saved)</option>}
                {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
              </select>
              {account?.connected && <span className="field__hint" role="status">
                {!voiceList ? 'Loading your Suno voices…' : voiceList.error || (unknownVoice
                  ? 'Choose a voice available in this account.' : !voices.length
                    ? 'Add a Voice in Suno, then reopen this dialog.' : 'Your own and liked Suno Voices.')}
              </span>}
            </label>}
          </div>

          <details open={shortenedSound || form.lyrics.length > 5000 || undefined}>
            <summary>{hasVocals ? 'Sound and lyrics' : 'Sound'}</summary>
            <div className="suno-settings__text stack gap-12">
              <label className="field">
                <span className="field__label">Sound</span>
                <textarea className="textarea" rows={3} maxLength={SOUND_LIMIT} value={form.caption} onChange={change('caption')} />
                {shortenedSound && <span className="field__hint">Sound was shortened to fit Suno’s 1,000-character limit. You can edit it here.</span>}
              </label>
              {hasVocals && <label className="field">
                <span className="field__label">Lyrics</span>
                <textarea className="textarea" rows={5} maxLength={5000} value={form.lyrics} onChange={change('lyrics')} />
                {form.lyrics.length > 5000 && <span className="field__hint">Shorten the lyrics to 5,000 characters for Suno.</span>}
              </label>}
            </div>
          </details>

          <fieldset className="suno-settings__advanced stack gap-10" disabled={!account?.connected}>
            <legend>Generation settings</legend>
            {account?.connected && !advanced && <span className="field__hint">Some controls require an eligible Suno plan.</span>}
            <div className="row gap-12 row--wrap">
              {hasVocals && <Choice label="Vocal gender" value={form.voice_id ? '' : form.vocal_gender}
                options={form.voice_id ? [['', 'From selected voice']] : [['', 'Auto'], ['female', 'Female'], ['male', 'Male']]}
                onChange={(value) => set('vocal_gender', value)} disabled={!advanced || !!form.voice_id} />}
              <Choice label="Duration" value={form.durationMode} options={[["auto", "Auto"], ["custom", "Custom"]]}
                onChange={(value) => set('durationMode', value)} />
              {custom && <label className="field">
                <span className="field__label">Seconds</span>
                <input className="input suno-settings__seconds" type="number" min={15} max={360} required
                  value={form.duration} onChange={change('duration')} />
              </label>}
            </div>
            <label className="check">
              <input type="checkbox" checked={form.max_mode} onChange={(e) => set('max_mode', e.target.checked)} />
              <span>Max Mode <span className="field__hint">· Uses extra Suno credits</span></span>
            </label>
            <Slider label="Weirdness" value={form.weirdness} onChange={(v) => set('weirdness', v)} disabled={!advanced} />
            <Slider label="Style influence" value={form.style_influence} onChange={(v) => set('style_influence', v)} disabled={!advanced} />
            <Slider label="Variety" value={form.variety} onChange={(v) => set('variety', v)} choices={VARIETY} />
            {hasVocals && form.voice_id && advanced && <Slider label="Audio influence"
              value={form.audio_influence} onChange={(v) => set('audio_influence', v)} />}
            <label className="field">
              <span className="field__label">Exclude styles</span>
              <input className="input" value={form.negative_style} maxLength={1000} onChange={change('negative_style')}
                disabled={!advanced} placeholder="e.g. accordion, polka" />
            </label>
          </fieldset>
        </div>
        <footer className="suno-settings__footer">
          <span className="field__hint">Creates two takes using Suno credits.</span>
          <div className="row gap-8">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" type="submit" disabled={!canGenerate || checking}>
              {checking ? 'Checking Suno connection…' : 'Generate two takes'}
            </Button>
          </div>
        </footer>
      </form>
    </dialog>
  )
}

export default function SunoGeneration({ defaults, caption = '', lyrics = defaults?.lyrics || '', busy, disabled, onGenerate }) {
  const [open, setOpen] = useState(false)
  const [retry, setRetry] = useState(null)
  const previous = retry?.caption === caption && retry?.lyrics === lyrics ? retry.form : null
  return (
    <>
      <Button icon="music" disabled={disabled || !defaults} onClick={() => setOpen(true)}>
        {busy ? 'Generating on Suno…' : 'Generate on Suno'}
      </Button>
      {open && <SunoDialog defaults={defaults} caption={caption} lyrics={lyrics} previous={previous}
        onClose={() => setOpen(false)} onGenerate={async (options, form) => {
          setOpen(false)
          setRetry(await onGenerate(options) === false ? { caption, lyrics, form } : null)
        }} />}
    </>
  )
}
