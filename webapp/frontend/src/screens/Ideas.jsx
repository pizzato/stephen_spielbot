import { useState, useEffect } from 'react'
import { Card, Chip, Button, Segmented, Icon, Banner, composeResolution, resolutionTier } from '../components.jsx'
import { api } from '../api.js'

function Stars({ value }) {
  if (value == null) return null
  return <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(value).toFixed(1)}</span>
}
function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(n)
}

// Per-idea predicted 3-day reach (issue #50). Renders nothing until an
// engagement model has been built, so it's a graceful no-op by default.
// Re-estimates when the chosen video length flips Short ↔ long-form.
function IdeaReach({ idea, isShort }) {
  const [r, setR] = useState(null)
  useEffect(() => {
    let live = true
    api.engagementPredict({ title: idea.title || idea.final_title || '', description: idea.reason || '', is_short: isShort })
      .then((d) => { if (live) setR(d) }).catch(() => {})
    return () => { live = false }
  }, [idea, isShort])
  if (!r?.available) return null
  return <Chip tone="accent"><Icon name="chart-line" style={{ fontSize: 10 }} /> ~{fmtNum(r.predicted_views)}</Chip>
}

// Orientation follows the video length (short → portrait, else landscape); the
// pixel/quality tier follows the configured default so AI-idea and audience
// suggestions queue at the default resolution rather than a hardcoded 1080p.
const LENGTH_PRESETS = {
  short:  { scenes: 6,  orientation: 'Portrait' },
  medium: { scenes: 12, orientation: 'Landscape' },
  long:   { scenes: 20, orientation: 'Landscape' },
}

const DISMISSED_IDEAS_KEY = 'spielbot.dismissedIdeas'

const normalizeIdeaTitle = (title) => String(title || '').trim().toLowerCase().replace(/\s+/g, ' ')
const readDismissedIdeas = () => {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(DISMISSED_IDEAS_KEY) || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}
const writeDismissedIdea = (idea, reason = 'dismissed') => {
  const title = idea?.title || idea?.final_title || idea
  const data = readDismissedIdeas()
  const record = { id: idea?.id || '', title, reason, dismissed_at: Date.now() }
  if (idea?.id) data[idea.id] = record
  const titleKey = normalizeIdeaTitle(title)
  if (titleKey) data[titleKey] = record
  window.localStorage.setItem(DISMISSED_IDEAS_KEY, JSON.stringify(data))
}
const isDismissedIdea = (idea) => {
  const title = idea?.title || idea?.final_title || idea
  const data = readDismissedIdeas()
  return Boolean((idea?.id && data[idea.id]) || data[normalizeIdeaTitle(title)])
}
const visibleIdeas = (ideas) => (ideas || []).filter((idea) => !isDismissedIdea(idea))

export default function Ideas({ go, meta = {} }) {
  const [ideas, setIdeas] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [loadingIdeas, setLoadingIdeas] = useState(false)
  const [guidance, setGuidance] = useState('')
  const [busy, setBusy] = useState('')          // action key currently running

  // The text box steers generation (e.g. "Rock bands of the 90s" → ideas about
  // 90s rock bands); blank = general ideas from the channel's gaps.
  const loadIdeas = async (g = '', refresh = false) => {
    setLoadingIdeas(true); setError('')
    try { const d = await api.getSuggestions(g, refresh); setIdeas(visibleIdeas(d.suggestions || [])) }
    catch (e) { setError(e.message) } finally { setLoadingIdeas(false) }
  }
  // First visit loads the cached set (no LLM call); only regenerates if empty.
  useEffect(() => { if (ideas.length === 0 && !loadingIdeas) loadIdeas('', false) }, [])

  const ideaKey = (idea) => idea?.id || idea?.title || idea?.final_title || ''
  const removeIdeaLocal = (idea) => {
    const key = ideaKey(idea)
    const title = idea?.title || idea?.final_title || idea
    setIdeas((arr) => arr.filter((it) => ideaKey(it) !== key && (it.title || it.final_title || it) !== title))
  }
  // Per-idea video length: stored on the idea itself so each card keeps its own
  // choice. New ideas have no length and default to 'medium' until toggled.
  const ideaLength = (idea) => idea?.length || 'medium'
  const setIdeaLength = (idea, length) => {
    const key = ideaKey(idea)
    setIdeas((arr) => arr.map((it) => (ideaKey(it) === key ? { ...it, length } : it)))
  }
  const closeIdea = async (idea, reason = 'dismissed') => {
    const title = idea.title || idea.final_title || idea
    const key = ideaKey(idea)
    setBusy('idea-' + key); setError('')
    writeDismissedIdea(idea, reason)
    removeIdeaLocal(idea)
    try {
      const r = await api.dismissSuggestion({ id: idea.id || '', title, reason })
      if (Array.isArray(r.suggestions)) setIdeas(visibleIdeas(r.suggestions))
      setStatus(reason === 'used' ? 'Idea marked as used.' : 'Idea closed.')
    } catch (e) {
      setStatus(reason === 'used' ? 'Idea marked as used.' : 'Idea closed.')
    } finally {
      setBusy('')
    }
  }
  // Resolution = the configured default's pixel tier at the length's orientation.
  // Length only chooses orientation; the quality tier follows the default
  // resolution (Settings), so AI-idea / suggestion videos no longer hardcode 1080p.
  const presetResolution = (length) => {
    const preset = LENGTH_PRESETS[length]
    const configured = meta.config?.resolution || meta.default_resolution || ''
    const tier = resolutionTier(meta, configured) || meta.default_pixels
    return composeResolution(meta, preset.orientation, tier) || configured
  }
  const queueIdea = async (idea, title) => {
    const { scenes } = LENGTH_PRESETS[ideaLength(idea)]
    const resolution = presetResolution(ideaLength(idea))
    setError('')
    try {
      await api.queueAdd(title, scenes, idea.reason || '', resolution)
      await closeIdea(idea, 'used')
      setStatus('Added to queue.')
      go('queue')
    } catch (e) {
      setError(e.message)
    }
  }
  const createIdea = async (idea, title) => {
    const { scenes } = LENGTH_PRESETS[ideaLength(idea)]
    const resolution = presetResolution(ideaLength(idea))
    await closeIdea(idea, 'used')
    go('create', { title, description: idea.reason || '', scenes, resolution })
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">AI ideas</span>
          <h1 className="display-md reveal reveal-d1">Topic ideas for your channel</h1>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center gap-10">
            <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="lightbulb" /></span>
            <div><div style={{ fontWeight: 600 }}>Topic ideas</div><div className="muted" style={{ fontSize: 12.5 }}>Type a theme to steer the AI, or leave it blank for ideas from your channel's gaps.</div></div>
          </div>
          <div className="row gap-10 center mt-16" style={{ flexWrap: 'wrap' }}>
            <div className="grow">
              <input className="input" placeholder="Guide the ideas — e.g. Rock bands of the 90s"
                value={guidance} onChange={(e) => setGuidance(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !loadingIdeas) loadIdeas(guidance, true) }} />
            </div>
            <Button variant="primary" icon="wand-magic-sparkles" disabled={loadingIdeas} onClick={() => loadIdeas(guidance, true)}>
              {loadingIdeas ? 'Thinking…' : (guidance.trim() ? 'Generate ideas' : 'Generate more')}</Button>
          </div>
        </Card>
        {ideas.map((idea, i) => {
          const title = idea.title || idea.final_title || idea
          const length = ideaLength(idea)
          const { scenes, orientation } = LENGTH_PRESETS[length]
          const key = ideaKey(idea) || `${title}-${i}`
          return (
            <Card key={key} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
              <div className="row center between">
                <span style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</span>
                <div className="row center gap-10"><IdeaReach idea={idea} isShort={length === 'short'} /><Stars value={idea.interestingness} /></div>
              </div>
              {idea.reason && <p className="muted" style={{ fontSize: 13, margin: '10px 0 0', fontStyle: 'italic' }}>{idea.reason}</p>}
              <div className="row center mt-16">
                <Segmented value={length} onChange={(v) => setIdeaLength(idea, v)} options={[
                  { value: 'short',  label: 'Short' },
                  { value: 'medium', label: 'Medium' },
                  { value: 'long',   label: 'Long' },
                ]} />
              </div>
              <div className="row center between mt-16 row--wrap gap-10">
                <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {orientation}</span>
                <div className="row gap-10">
                  <Button variant="ghost" icon="xmark" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea)}>Close</Button>
                  <Button variant="ghost" icon="layer-group" disabled={busy === 'idea-' + key} onClick={() => queueIdea(idea, title)}>Queue</Button>
                  <Button variant="primary" icon="wand-magic-sparkles" disabled={busy === 'idea-' + key} onClick={() => createIdea(idea, title)}>Create</Button>
                </div>
              </div>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
