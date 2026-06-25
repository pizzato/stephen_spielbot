import { useState, useEffect, useRef, useMemo } from 'react'
import { Card, Chip, Button, Segmented, Icon, Banner } from '../components.jsx'
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
function IdeaReach({ idea, isShort, onResult }) {
  const [r, setR] = useState(null)
  // Report the prediction up so the list can sort by predicted views, without
  // re-fetching when the (per-render) callback identity changes.
  const report = useRef(onResult)
  report.current = onResult
  useEffect(() => {
    let live = true
    api.engagementPredict({ title: idea.title || idea.final_title || '', description: idea.reason || '', is_short: isShort, style_name: idea.style_name || '' })
      .then((d) => { if (live) { setR(d); report.current?.(d) } }).catch(() => {})
    return () => { live = false }
  }, [idea, isShort])
  if (!r?.available) return null
  return <Chip tone="accent"><Icon name="chart-line" style={{ fontSize: 10 }} /> ~{fmtNum(r.predicted_views)}</Chip>
}

// AI-ideas sort options. Predicted views falls back to newest when a model
// hasn't produced a value for an idea yet (predictions arrive asynchronously).
const SORT_OPTIONS = [
  { value: 'newest', label: 'Newest first' },
  { value: 'oldest', label: 'Oldest first' },
  { value: 'interesting', label: 'Most interesting' },
  { value: 'views', label: 'Predicted views' },
]

// Size presets (Small/Medium/Large) are configured per style in Settings; each
// pairs a scene count with a resolution. The toggle below picks one per idea.
const SIZE_ORDER = ['small', 'medium', 'large']
const SIZE_LABELS = { small: 'Small', medium: 'Medium', large: 'Large' }
// Ideas tagged before the rename used short/medium/long — map them across.
const LEGACY_SIZE = { short: 'small', medium: 'medium', long: 'large' }
const orientationOf = (resolution) => String(resolution || '').split(' ')[0]

// Sentinel style selection: show/generate a mix of ideas across every style.
// Kept in sync with ALL_STYLES on the backend.
const ALL_STYLES = '__all__'

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
const clearDismissedIdea = (idea) => {
  const title = idea?.title || idea?.final_title || idea
  const data = readDismissedIdeas()
  if (idea?.id) delete data[idea.id]
  const titleKey = normalizeIdeaTitle(title)
  if (titleKey) delete data[titleKey]
  window.localStorage.setItem(DISMISSED_IDEAS_KEY, JSON.stringify(data))
}
const isDismissedIdea = (idea) => {
  const title = idea?.title || idea?.final_title || idea
  const data = readDismissedIdeas()
  return Boolean((idea?.id && data[idea.id]) || data[normalizeIdeaTitle(title)])
}
const sameIdea = (a, b) =>
  (a?.id && b?.id && a.id === b.id) ||
  normalizeIdeaTitle(a?.title || a?.final_title || a) === normalizeIdeaTitle(b?.title || b?.final_title || b)
const visibleIdeas = (ideas) => (ideas || []).filter((idea) => !isDismissedIdea(idea))

export default function Ideas({ go, meta = {} }) {
  const [ideas, setIdeas] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [loadingIdeas, setLoadingIdeas] = useState(false)
  const [guidance, setGuidance] = useState('')
  const [busy, setBusy] = useState('')          // action key currently running
  const [sortBy, setSortBy] = useState('newest')
  const [preds, setPreds] = useState({})        // ideaKey -> engagement prediction, for the "Predicted views" sort
  const [discarded, setDiscarded] = useState([]) // ideas the user closed — kept out of suggestions, revivable
  const [showDiscarded, setShowDiscarded] = useState(false)

  // Ideas belong to a style profile (issue #66): generation is steered by the
  // selected style and each idea is stamped with it, so a children-story style
  // gets children-story topics. '' = the default style (resolved server-side);
  // ALL_STYLES = a mix of every style, shown together so you can pick across them.
  const [styleSel, setStyleSel] = useState('')
  const styleList = meta.config?.styles || []
  const isAll = styleSel === ALL_STYLES
  const effectiveStyle = styleSel || meta.config?.default_style || ''
  // Styles opted out of auto-picked ideas stay out of the "All styles" mix
  // (reach them by selecting the style itself), mirroring the backend.
  const excludedStyles = new Set(styleList.filter((s) => s.auto_pick_exclude).map((s) => s.name))
  // The style an idea (and its size preset / queue entry) belongs to — its own
  // stamp in the mix, otherwise the selected style.
  const styleOf = (idea) => idea?.style_name || (isAll ? (meta.config?.default_style || '') : effectiveStyle)
  const byStyle = (arr) => (arr || []).filter((i) => {
    const sn = i.style_name || meta.config?.default_style
    return isAll ? !excludedStyles.has(sn) : (!effectiveStyle || sn === effectiveStyle)
  })

  // The text box steers generation (e.g. "Rock bands of the 90s" → ideas about
  // 90s rock bands); blank = general ideas from the channel's gaps.
  const loadIdeas = async (g = '', refresh = false, styleName = styleSel) => {
    setLoadingIdeas(true); setError('')
    try {
      const d = await api.getSuggestions(g, refresh, styleName)
      setIdeas(visibleIdeas(d.suggestions || []))
    } catch (e) { setError(e.message) } finally { setLoadingIdeas(false) }
  }
  // Discarded ideas for the current style ('' style → its default; All styles → every discard).
  const discardStyleArg = (sel) => (sel === ALL_STYLES ? '' : (sel || meta.config?.default_style || ''))
  const loadDiscarded = async (sel = styleSel) => {
    try {
      const d = await api.getDiscarded(discardStyleArg(sel))
      setDiscarded(d.discarded || [])
    } catch { /* non-fatal — the discard list is supplementary */ }
  }
  // First visit loads the cached set (no LLM call); only regenerates if empty.
  useEffect(() => { if (ideas.length === 0 && !loadingIdeas) loadIdeas('', false); loadDiscarded() }, [])
  // Switching style swaps to that style's cached ideas (generates when empty).
  const pickStyle = (name) => {
    setStyleSel(name)
    setIdeas([])
    loadIdeas(guidance, false, name)
    loadDiscarded(name)
  }

  const ideaKey = (idea) => idea?.id || idea?.title || idea?.final_title || ''
  const removeIdeaLocal = (idea) => {
    const key = ideaKey(idea)
    const title = idea?.title || idea?.final_title || idea
    setIdeas((arr) => arr.filter((it) => ideaKey(it) !== key && (it.title || it.final_title || it) !== title))
  }
  // Per-idea size: stored on the idea itself so each card keeps its own choice.
  // New ideas have no size and default to 'small' until toggled.
  const ideaSize = (idea) => {
    const v = idea?.size || idea?.length || 'small'
    return LEGACY_SIZE[v] || v
  }
  const setIdeaSize = (idea, size) => {
    const key = ideaKey(idea)
    setIdeas((arr) => arr.map((it) => (ideaKey(it) === key ? { ...it, size } : it)))
  }
  // Remove an idea from the active list. reason: 'declined' → tracked in the
  // reviewable "Declined" list (the AI steers away from it); 'ignored' → hidden
  // for good, never shown again, but kept out of the Declined list; 'used' →
  // queued/created. Decline and Ignore both keep it out of future suggestions.
  const dismissLabel = (reason) =>
    reason === 'used' ? 'Idea marked as used.'
      : reason === 'ignored' ? 'Idea ignored.'
        : 'Idea declined.'
  const closeIdea = async (idea, reason = 'declined') => {
    const title = idea.title || idea.final_title || idea
    const key = ideaKey(idea)
    setBusy('idea-' + key); setError('')
    writeDismissedIdea(idea, reason)
    removeIdeaLocal(idea)
    try {
      const r = await api.dismissSuggestion({ id: idea.id || '', title, reason })
      if (Array.isArray(r.suggestions)) setIdeas(byStyle(visibleIdeas(r.suggestions)))
      if (reason === 'declined') loadDiscarded()   // surface it under "Declined"
      setStatus(dismissLabel(reason))
    } catch (e) {
      setStatus(dismissLabel(reason))
    } finally {
      setBusy('')
    }
  }
  // Scenes + resolution come straight from the idea's style size preset, so each
  // size is exactly what that style configured in Settings.
  const presetFor = (idea, size) => {
    const styleObj = styleList.find((s) => s.name === styleOf(idea))
    const presets = styleObj?.size_presets || meta.default_size_presets || {}
    return presets[size] || (meta.default_size_presets || {})[size]
      || { scenes: 6, resolution: meta.default_resolution || '' }
  }
  const queueIdea = async (idea, title) => {
    const { scenes, resolution } = presetFor(idea, ideaSize(idea))
    setError('')
    try {
      await api.queueAdd(title, scenes, idea.reason || '', resolution, styleOf(idea))
      await closeIdea(idea, 'used')
      setStatus('Added to queue.')
    } catch (e) {
      setError(e.message)
    }
  }
  const createIdea = async (idea, title) => {
    const { scenes, resolution } = presetFor(idea, ideaSize(idea))
    await closeIdea(idea, 'used')
    go('create', { title, description: idea.reason || '', scenes, resolution, styleName: styleOf(idea) })
  }
  // Bring a discarded idea back into the active list. Clear the local hide too,
  // otherwise visibleIdeas would re-filter it straight back out.
  const reviveIdea = async (rec) => {
    setError('')
    clearDismissedIdea(rec)
    setDiscarded((arr) => arr.filter((r) => !sameIdea(r, rec)))
    try {
      await api.reviveSuggestion({ id: rec.id || '', title: rec.title || '' })
      await loadIdeas(guidance, false)
      setStatus('Idea revived.')
    } catch (e) { setError(e.message); loadDiscarded() }
  }
  // Forget a discarded idea for good — drops it from the discard list (it may
  // resurface organically in a future generation).
  const forgetIdea = async (rec) => {
    setError('')
    setDiscarded((arr) => arr.filter((r) => !sameIdea(r, rec)))
    try {
      await api.forgetSuggestion({ id: rec.id || '', title: rec.title || '' })
      setStatus('Idea forgotten.')
    } catch (e) { setError(e.message); loadDiscarded() }
  }

  // Sort is view-only — it reorders the cards, it never drops an idea (ideas
  // leave only on Close/Queue/Create). Predicted views read from the per-card
  // reach lookups; ideas with no prediction yet fall back to newest.
  const sortedIdeas = useMemo(() => {
    const pv = (idea) => preds[ideaKey(idea)]?.predicted_views
    const byNewest = (a, b) => (b.created_at || 0) - (a.created_at || 0)
    const cmp = {
      newest: byNewest,
      oldest: (a, b) => (a.created_at || 0) - (b.created_at || 0),
      interesting: (a, b) => (b.interestingness || 0) - (a.interestingness || 0),
      views: (a, b) => ((pv(b) ?? -1) - (pv(a) ?? -1)) || byNewest(a, b),
    }[sortBy] || byNewest
    return [...ideas].sort(cmp)
  }, [ideas, sortBy, preds])

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
            <div><div style={{ fontWeight: 600 }}>Topic ideas</div><div className="muted" style={{ fontSize: 12.5 }}>Ideas are generated for a style — pick one, then type a theme to steer the AI or leave it blank for ideas from your channel's gaps.</div></div>
          </div>
          <div className="row gap-10 center mt-16" style={{ flexWrap: 'wrap' }}>
            {styleList.length > 0 && (
              <select className="select" value={isAll ? ALL_STYLES : effectiveStyle} onChange={(e) => pickStyle(e.target.value)}
                disabled={loadingIdeas} style={{ maxWidth: 220 }} title="Ideas are generated for this style">
                {styleList.length > 1 && <option value={ALL_STYLES}>All styles (mix)</option>}
                {styleList.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}{meta.config?.default_style === s.name ? ' (default)' : ''}
                  </option>
                ))}
              </select>
            )}
            <select className="select" value={sortBy} onChange={(e) => setSortBy(e.target.value)}
              style={{ maxWidth: 180 }} title="Sort the ideas">
              {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <div className="grow">
              <input className="input" placeholder="Guide the ideas — e.g. Rock bands of the 90s"
                value={guidance} onChange={(e) => setGuidance(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !loadingIdeas) loadIdeas(guidance, true) }} />
            </div>
            <Button variant="primary" icon="wand-magic-sparkles" disabled={loadingIdeas} onClick={() => loadIdeas(guidance, true)}>
              {loadingIdeas ? 'Thinking…' : (guidance.trim() ? 'Generate ideas' : 'Generate more')}</Button>
          </div>
        </Card>
        {sortedIdeas.map((idea, i) => {
          const title = idea.title || idea.final_title || idea
          const size = ideaSize(idea)
          const { scenes, resolution } = presetFor(idea, size)
          const key = ideaKey(idea) || `${title}-${i}`
          const pk = ideaKey(idea)
          return (
            <Card key={key} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
              <div className="row center between">
                <span style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</span>
                <div className="row center gap-10">{isAll && idea.style_name && <Chip>{idea.style_name}</Chip>}<IdeaReach idea={idea} isShort={orientationOf(resolution) === 'Portrait'} onResult={(d) => setPreds((m) => ({ ...m, [pk]: d }))} /><Stars value={idea.interestingness} /></div>
              </div>
              {idea.reason && <p className="muted" style={{ fontSize: 13, margin: '10px 0 0', fontStyle: 'italic' }}>{idea.reason}</p>}
              <div className="row center mt-16">
                <Segmented value={size} onChange={(v) => setIdeaSize(idea, v)}
                  options={SIZE_ORDER.map((s) => ({ value: s, label: SIZE_LABELS[s] }))} />
              </div>
              <div className="row center between mt-16 row--wrap gap-10">
                <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {orientationOf(resolution)}</span>
                <div className="row gap-10 row--wrap">
                  <Button variant="ghost" icon="ban" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea, 'declined')} title="Add to the not-accepted list so the AI steers away from it">Decline</Button>
                  <Button variant="ghost" icon="eye-slash" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea, 'ignored')} title="Hide it for good — you won't see it again">Ignore</Button>
                  <Button variant="ghost" icon="layer-group" disabled={busy === 'idea-' + key} onClick={() => queueIdea(idea, title)}>Queue</Button>
                  <Button variant="primary" icon="wand-magic-sparkles" disabled={busy === 'idea-' + key} onClick={() => createIdea(idea, title)}>Create</Button>
                </div>
              </div>
            </Card>
          )
        })}
        {discarded.length > 0 && (
          <Card span={12} well className="reveal">
            <div className="row center between" style={{ cursor: 'pointer' }} onClick={() => setShowDiscarded((v) => !v)}>
              <div className="row center gap-10">
                <span className="stream-ico" style={{ background: 'var(--surface-2)', color: 'var(--muted)' }}><Icon name="trash-can" /></span>
                <div>
                  <div style={{ fontWeight: 600 }}>Declined ideas ({discarded.length})</div>
                  <div className="muted" style={{ fontSize: 12.5 }}>Topics you turned down — kept out of new suggestions. Revive one to bring it back, or forget it for good. (Ignored ideas stay hidden and aren't listed here.)</div>
                </div>
              </div>
              <Icon name={showDiscarded ? 'chevron-up' : 'chevron-down'} />
            </div>
            {showDiscarded && (
              <div className="mt-16">
                {discarded.map((rec) => (
                  <div key={rec.id || rec.title} className="row center between row--wrap gap-10"
                    style={{ padding: '10px 0', borderTop: '1px solid var(--line)' }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 600 }}>{rec.title}{isAll && rec.style_name ? <span style={{ marginLeft: 8 }}><Chip>{rec.style_name}</Chip></span> : null}</div>
                      {rec.reason && <div className="muted" style={{ fontSize: 12.5, fontStyle: 'italic' }}>{rec.reason}</div>}
                    </div>
                    <div className="row gap-10">
                      <Button variant="ghost" icon="rotate-left" onClick={() => reviveIdea(rec)}>Revive</Button>
                      <Button variant="ghost" icon="trash-can" onClick={() => forgetIdea(rec)}>Forget</Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>
        )}
      </div>
    </div>
  )
}
