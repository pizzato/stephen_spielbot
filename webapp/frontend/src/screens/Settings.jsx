import React, { useState, useEffect } from 'react'
import { Card, Field, Segmented, Check, Button, Banner } from '../components.jsx'
import { api } from '../api.js'

const toLines = (v) => Array.isArray(v) ? v.join('\n') : (v || '')
const fromLines = (s) => (s || '').split('\n').map((x) => x.trim()).filter(Boolean)

export default function Settings({ meta, setMeta }) {
  const [cfg, setCfg] = useState(meta.config || {})
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { setCfg(meta.config || {}) }, [meta.config])

  const set = (k, v) => setCfg((c) => ({ ...c, [k]: v }))

  const save = async () => {
    setBusy(true); setError(''); setStatus('')
    try {
      const out = { ...cfg }
      out.comfy_workers = fromLines(toLines(cfg.comfy_workers))
      out.tts_workers = fromLines(toLines(cfg.tts_workers))
      const r = await api.saveConfig(out)
      setStatus('Settings saved.')
      setMeta((m) => ({ ...m, config: r.config }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const isClaude = cfg.llm_backend === 'claude'

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Settings</span>
          <h1 className="display-md reveal reveal-d1">Studio configuration</h1>
        </div>
        <Button variant="primary" icon="floppy-disk" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save settings'}</Button>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        <Card span={6} className="reveal reveal-d1">
          <span className="label-sm">Render quality</span>
          <div className="stack gap-22 mt-16">
            <Field label="Resolution" hint="Higher = better quality, slower.">
              <select className="select" value={cfg.resolution || ''} onChange={(e) => set('resolution', e.target.value)}>
                {(meta.resolutions || []).map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </Field>
            <div className="row gap-22 row--wrap">
              <div className="grow"><Field label="First-pass steps" hint="8 distilled · 20–30 dev model.">
                <input className="input" type="number" value={cfg.first_pass_steps ?? ''} onChange={(e) => set('first_pass_steps', +e.target.value)} /></Field></div>
              <div className="grow"><Field label="Second-pass steps">
                <input className="input" type="number" value={cfg.second_pass_steps ?? ''} onChange={(e) => set('second_pass_steps', +e.target.value)} /></Field></div>
            </div>
            <Field label={`LoRA strength — ${cfg.lora_strength ?? 0}`}>
              <input className="slider" type="range" min={0} max={1} step={0.05} value={cfg.lora_strength ?? 0} onChange={(e) => set('lora_strength', +e.target.value)} />
            </Field>
          </div>
        </Card>

        <Card span={6} className="reveal reveal-d2">
          <span className="label-sm">Infrastructure</span>
          <div className="stack gap-22 mt-16">
            <Field label="ComfyUI workers" hint="One URL per line.">
              <textarea className="textarea" rows={3} value={toLines(cfg.comfy_workers)} onChange={(e) => set('comfy_workers', e.target.value)} />
            </Field>
            <Field label="TTS workers" hint="One host per line.">
              <textarea className="textarea" rows={2} value={toLines(cfg.tts_workers)} onChange={(e) => set('tts_workers', e.target.value)} />
            </Field>
          </div>
        </Card>

        <Card span={6} className="reveal reveal-d3">
          <span className="label-sm">LLM backend</span>
          <div className="stack gap-22 mt-16">
            <Field label="Backend">
              <Segmented value={cfg.llm_backend || 'local'} onChange={(v) => set('llm_backend', v)}
                options={[{ value: 'local', label: 'Local (vLLM)' }, { value: 'claude', label: 'Claude' }]} />
            </Field>
            {isClaude ? (
              <>
                <Field label="Claude API key"><input className="input" type="password" value={cfg.claude_api_key || ''} onChange={(e) => set('claude_api_key', e.target.value)} /></Field>
                <Field label="Claude model"><input className="input" value={cfg.claude_model || ''} onChange={(e) => set('claude_model', e.target.value)} /></Field>
              </>
            ) : (
              <>
                <Field label="Local LLM URL"><input className="input" value={cfg.local_llm_url || ''} onChange={(e) => set('local_llm_url', e.target.value)} /></Field>
                <Field label="Local LLM model"><input className="input" value={cfg.local_llm_model || ''} onChange={(e) => set('local_llm_model', e.target.value)} /></Field>
              </>
            )}
          </div>
        </Card>

        <Card span={6} className="reveal reveal-d3">
          <span className="label-sm">Script & content defaults</span>
          <div className="stack gap-22 mt-16">
            <div className="row gap-22 row--wrap">
              <div className="grow"><Field label="Default scenes"><input className="input" type="number" value={cfg.default_n_scenes ?? ''} onChange={(e) => set('default_n_scenes', +e.target.value)} /></Field></div>
              <div className="grow"><Field label="Default voice"><select className="select" value={cfg.default_voice || ''} onChange={(e) => set('default_voice', e.target.value)}>
                <option value="">(F5-TTS default)</option>
                {(meta.voices || []).map((v) => <option key={v} value={v}>{v}</option>)}
              </select></Field></div>
            </div>
            <Field label="Default visual style"><input className="input" value={cfg.default_visual_style || ''} onChange={(e) => set('default_visual_style', e.target.value)} /></Field>
            <Field label="Extra script instructions" hint="Appended to every topic.">
              <textarea className="textarea" rows={2} value={cfg.script_extra_instructions || ''} onChange={(e) => set('script_extra_instructions', e.target.value)} />
            </Field>
          </div>
        </Card>

        <Card span={6} className="reveal reveal-d4">
          <span className="label-sm">Narrator & audio</span>
          <div className="stack gap-22 mt-16">
            {[['voice_vol', 'Voice volume', 150], ['music_vol', 'Music volume', 100], ['ambient_vol', 'Ambient volume', 100]].map(([k, label, max]) => (
              <Field key={k} label={`${label} — ${cfg[k] ?? 0}%`}>
                <input className="slider" type="range" min={0} max={max} value={cfg[k] ?? 0} onChange={(e) => set(k, +e.target.value)} />
              </Field>
            ))}
          </div>
        </Card>

        <Card span={6} className="reveal reveal-d4">
          <span className="label-sm">YouTube automation</span>
          <div className="stack gap-16 mt-16">
            <Check checked={!!cfg.youtube_fully_automated} onChange={(v) => set('youtube_fully_automated', v)} label="⚡ Fully automated mode" />
            <Check checked={!!cfg.youtube_auto_fetch_evaluate} onChange={(v) => set('youtube_auto_fetch_evaluate', v)} label="Fetch & evaluate comments on a schedule" />
            <Check checked={!!cfg.youtube_auto_approve_comments} onChange={(v) => set('youtube_auto_approve_comments', v)} label="Auto-approve requests above the confidence threshold" />
            <Check checked={!!cfg.youtube_auto_start_job} onChange={(v) => set('youtube_auto_start_job', v)} label="Auto-start rendering the highest-interest request" />
            <Check checked={!!cfg.youtube_auto_approve_script} onChange={(v) => set('youtube_auto_approve_script', v)} label="Auto-approve script (skip review)" />
            <Check checked={!!cfg.youtube_auto_post} onChange={(v) => set('youtube_auto_post', v)} label="Auto-post to YouTube when a film finishes" />
            <Field label="Default privacy">
              <Segmented value={cfg.youtube_post_privacy || 'private'} onChange={(v) => set('youtube_post_privacy', v)} options={['private', 'unlisted', 'public']} />
            </Field>
          </div>
        </Card>
      </div>
    </div>
  )
}
