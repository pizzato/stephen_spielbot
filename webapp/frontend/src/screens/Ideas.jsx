import { useState, useEffect, useRef, useMemo } from 'react'
import { Card, Chip, Button, Segmented, Icon, Banner, fmtDuration, LEGACY_SCENE_SECS } from '../components.jsx'
import { api } from '../api.js'
import { resolveStyle, styleTreeOrder } from '../styleUtils.js'

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

const sourceUrl = (value) => /^https?:\/\//i.test(value || '') ? value : ''
const newsTime = (value) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : 'Never'
const newsOutcome = (monitor) => ({
  no_posts: 'No matching X posts were returned.',
  no_new_posts: 'All matching posts were already checked.',
  no_ideas: 'New posts were found, but no usable video ideas were generated.',
  duplicates: 'Generated ideas were already present or previously reviewed.',
  ideas_added: `${monitor.ideas_added || 0} new idea${monitor.ideas_added === 1 ? '' : 's'} added.`,
  error: 'The news check failed. See the error below.',
}[monitor.last_outcome] || '')

function NewsDetails({ idea }) {
  if (idea.source !== 'news' && !idea.news) return null
  const news = idea.news || {}
  return (
    <div className="stack gap-8 mt-16" style={{ fontSize: 12.5 }}>
      <div><Chip tone="accent">From X news</Chip></div>
      {news.summary && <div>{news.summary}</div>}
      {(news.sources || []).map((post, index) => (
        <div key={post.id || index}>
          {sourceUrl(post.url) && <a href={post.url} target="_blank" rel="noopener noreferrer">{post.author_name || post.author || `Source ${index + 1}`} on X</a>}
          {post.created_at && <span className="muted"> · {newsTime(post.created_at)}</span>}
          {post.text && <div className="muted">{post.text}</div>}
          {(post.article_urls || []).filter(sourceUrl).map((url) => (
            <div key={url}><a href={url} target="_blank" rel="noopener noreferrer">Read linked article</a></div>
          ))}
        </div>
      ))}
      {!!news.people?.length && <div className="muted">
        People: {news.people.map((person) => person.name).join(', ')}.
        {news.include_people ? ' Reference pictures will be sought when creating the film.' : ' Appearance references are off for this idea.'}
      </div>}
    </div>
  )
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
  if (titleKey && idea?.source !== 'news') data[titleKey] = record
  window.localStorage.setItem(DISMISSED_IDEAS_KEY, JSON.stringify(data))
}
const clearDismissedIdea = (idea) => {
  const title = idea?.title || idea?.final_title || idea
  const data = readDismissedIdeas()
  if (idea?.id) delete data[idea.id]
  const titleKey = normalizeIdeaTitle(title)
  if (titleKey && idea?.source !== 'news') delete data[titleKey]
  window.localStorage.setItem(DISMISSED_IDEAS_KEY, JSON.stringify(data))
}
const isDismissedIdea = (idea) => {
  const title = idea?.title || idea?.final_title || idea
  const data = readDismissedIdeas()
  if (idea?.source === 'news') return Boolean(idea.id && data[idea.id])
  return Boolean((idea?.id && data[idea.id]) || data[normalizeIdeaTitle(title)])
}
const sameIdea = (a, b) =>
  (a?.id && b?.id && a.id === b.id) ||
  (a?.source !== 'news' && b?.source !== 'news' &&
    normalizeIdeaTitle(a?.title || a?.final_title || a) === normalizeIdeaTitle(b?.title || b?.final_title || b))
const visibleIdeas = (ideas) => (ideas || []).filter((idea) => !isDismissedIdea(idea))

export default function Ideas({ go, meta = {} }) {
  const [area, setArea] = useState('topics')
  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">AI ideas</span>
          <h1 className="display-md reveal reveal-d1">{area === 'news' ? 'News for your channel' : 'Topic ideas for your channel'}</h1>
        </div>
        <Segmented value={area} onChange={setArea} options={[
          { value: 'topics', label: 'Topic Ideas' },
          { value: 'news', label: 'News' },
        ]} />
      </div>
      <IdeasArea key={area} area={area} go={go} meta={meta} />
    </div>
  )
}

function IdeasArea({ area, go, meta }) {
  const isNews = area === 'news'
  const [ideas, setIdeas] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [loadingIdeas, setLoadingIdeas] = useState(false)
  const [guidance, setGuidance] = useState('')
  const [busy, setBusy] = useState('')          // action key currently running
  const [sortBy, setSortBy] = useState('newest')
  const [preds, setPreds] = useState({})        // ideaKey -> engagement prediction, for the "Predicted views" sort
  const [discarded, setDiscarded] = useState([]) // ideas the user declined — kept out of suggestions, revivable
  const [accepted, setAccepted] = useState([])   // ideas the user accepted — waiting to be queued/created
  const [rowSizes, setRowSizes] = useState({})   // accepted-row size overrides (recKey -> size)
  const [view, setView] = useState('ideas')      // 'ideas' | 'accepted' | 'declined'
  const [newsStatus, setNewsStatus] = useState(null)
  const [newsError, setNewsError] = useState('')
  const [checkingNews, setCheckingNews] = useState(false)

  // Ideas belong to a style profile (issue #66): generation is steered by the
  // selected style and each idea is stamped with it, so a children-story style
  // gets children-story topics. '' = the default style (resolved server-side);
  // ALL_STYLES = a mix of every style, shown together so you can pick across them.
  const [styleSel, setStyleSel] = useState('')
  const styleList = meta.config?.styles || []
  const isAll = styleSel === ALL_STYLES
  const effectiveStyle = styleSel || meta.config?.default_style || ''
  useEffect(() => {
    if (!isNews) return
    let live = true
    const refresh = () => api.newsStatus(styleSel)
      .then((data) => { if (live) { setNewsStatus(data); setNewsError('') } })
      .catch((e) => { if (live) setNewsError(e.message) })
    setNewsStatus(null)
    refresh()
    const timer = setInterval(refresh, 60000)
    return () => { live = false; clearInterval(timer) }
  }, [styleSel, isNews])
  // Styles opted out of auto-picked ideas stay out of the "All styles" mix
  // (reach them by selecting the style itself), mirroring the backend. A child
  // style inherits its parent's opt-out, so resolve through the chain.
  const excludedStyles = new Set(styleList
    .filter((s) => resolveStyle(styleList, s.name)?.auto_pick_exclude)
    .map((s) => s.name))
  // The style an idea (and its size preset / queue entry) belongs to — its own
  // stamp in the mix, otherwise the selected style.
  const styleOf = (idea) => idea?.style_name || (isAll ? (meta.config?.default_style || '') : effectiveStyle)
  const byStyle = (arr, selection = styleSel, excludeOptedOut = true) => (arr || []).filter((i) => {
    if ((i.source === 'news') !== isNews) return false
    const sn = i.style_name || meta.config?.default_style
    const selectedStyle = selection || meta.config?.default_style || ''
    return selection === ALL_STYLES ? (isNews || !excludeOptedOut || !excludedStyles.has(sn)) : (!selectedStyle || sn === selectedStyle)
  })

  // The text box steers generation (e.g. "Rock bands of the 90s" → ideas about
  // 90s rock bands); blank = general ideas from the channel's gaps.
  const loadIdeas = async (g = '', refresh = false, styleName = styleSel) => {
    setLoadingIdeas(true); setError('')
    try {
      const d = await (isNews ? api.getNewsIdeas(styleName) : api.getSuggestions(g, refresh, styleName))
      setIdeas(byStyle(visibleIdeas(d.suggestions || []), styleName))
    } catch (e) { setError(e.message) } finally { setLoadingIdeas(false) }
  }
  // Accepted/declined ideas for the current style ('' style → its default;
  // All styles → every record).
  const discardStyleArg = (sel) => (sel === ALL_STYLES ? '' : (sel || meta.config?.default_style || ''))
  const loadDiscarded = async (sel = styleSel) => {
    try {
      const d = await api.getDiscarded(discardStyleArg(sel))
      setDiscarded(byStyle(d.discarded || [], sel, false))
    } catch { /* non-fatal — the declined list is supplementary */ }
  }
  const loadAccepted = async (sel = styleSel) => {
    try {
      const d = await api.getAccepted(discardStyleArg(sel))
      setAccepted(byStyle(d.accepted || [], sel, false))
    } catch { /* non-fatal — the accepted list is supplementary */ }
  }
  const checkNews = async () => {
    setCheckingNews(true); setNewsError(''); setStatus('')
    try {
      const result = await api.checkNews(styleSel)
      const current = await api.newsStatus(styleSel)
      setNewsStatus(current)
      await Promise.all([loadIdeas('', false), loadAccepted()])
      if (result.running) {
        setStatus('A news check is already running. Check the monitor below for its results.')
      } else if ((result.styles || current.styles || []).some((monitor) => monitor.enabled && monitor.last_error)) {
        setNewsError('The news check reported errors. See the affected styles below.')
      } else {
        setStatus(`News check finished — ${result.ideas_added || 0} new idea${result.ideas_added === 1 ? '' : 's'}. See each style below for the search results.`)
      }
    } catch (e) { setNewsError(e.message) } finally { setCheckingNews(false) }
  }
  // News only loads saved ideas. Topic ideas generate a batch when the cache is empty.
  useEffect(() => { if (ideas.length === 0 && !loadingIdeas) loadIdeas('', false); loadDiscarded(); loadAccepted() }, [])
  // Switching style swaps to that style's cached ideas (generates when empty).
  // The guidance box is cleared with it (issue #202): it was steering the style
  // you just left, and carrying it over generates off-theme ideas for the new
  // one whenever that style has nothing cached. Pass '' rather than `guidance`
  // — the setGuidance above doesn't apply until the next render.
  const pickStyle = (name) => {
    setStyleSel(name)
    setIdeas([])
    setAccepted([])
    setDiscarded([])
    setStatus('')
    setGuidance('')
    loadIdeas('', false, name)
    loadDiscarded(name)
    loadAccepted(name)
  }

  const ideaKey = (idea) => idea?.id || idea?.title || idea?.final_title || ''
  const removeIdeaLocal = (idea) => {
    setIdeas((arr) => arr.filter((it) => !sameIdea(it, idea)))
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
  // Remove an idea from the active list. reason: 'accepted' → tracked in the
  // reviewable "Accepted" list, ready to queue/create; 'declined' → tracked in
  // the reviewable "Declined" list (the AI steers away from it); 'ignored' →
  // hidden for good, never shown again, but kept out of the Declined list.
  // All three keep the topic out of future suggestions.
  const dismissLabel = (reason) =>
    reason === 'accepted' ? 'Idea accepted — find it under Accepted.'
      : reason === 'ignored' ? 'Idea ignored.'
        : 'Idea declined.'
  const closeIdea = async (idea, reason = 'declined') => {
    const title = idea.title || idea.final_title || idea
    const key = ideaKey(idea)
    setBusy('idea-' + key); setError('')
    writeDismissedIdea(idea, reason)
    removeIdeaLocal(idea)
    try {
      const body = { id: idea.id || '', title, reason }
      if (reason === 'accepted') body.size = ideaSize(idea)   // keep the card's size choice
      const r = await api.dismissSuggestion(body)
      if (Array.isArray(r.suggestions)) setIdeas(byStyle(visibleIdeas(r.suggestions)))
      if (reason === 'declined') loadDiscarded()   // surface it under "Declined"
      if (reason === 'accepted') loadAccepted()    // surface it under "Accepted"
      setStatus(dismissLabel(reason))
    } catch (e) {
      setStatus(dismissLabel(reason))
    } finally {
      setBusy('')
    }
  }
  // Move an idea between the Accepted and Declined lists — re-dismiss it with
  // the other reason (the backend resets any acted-upon marker).
  const moveIdea = async (rec, reason) => {
    setError('')
    writeDismissedIdea(rec, reason)
    try {
      await api.dismissSuggestion({ id: rec.id || '', title: rec.title || '', reason })
      setStatus(reason === 'accepted' ? 'Moved to Accepted.' : 'Moved to Declined.')
    } catch (e) { setError(e.message) }
    loadDiscarded(); loadAccepted()
  }
  // Length + resolution come straight from the idea's style size preset, so
  // each size is exactly what that style configured in Settings. Minutes are
  // authoritative; a pre-minutes preset falls back to its legacy scene count.
  const presetFor = (idea, size) => {
    const styleObj = resolveStyle(styleList, styleOf(idea))
    const presets = styleObj?.size_presets || meta.default_size_presets || {}
    const p = presets[size] || (meta.default_size_presets || {})[size]
      || { minutes: 1, scenes: 6, resolution: meta.default_resolution || '' }
    const minutes = p.minutes || Math.round(((p.scenes || 6) * LEGACY_SCENE_SECS / 60) * 100) / 100
    return { ...p, minutes }
  }
  // Accepted rows keep their own size choice, seeded from the size chosen on
  // the card at accept time (falls back to 'small').
  const recKey = (rec) => rec?.id || rec?.title || ''
  const recSize = (rec) => {
    const v = rowSizes[recKey(rec)] || rec?.size || 'small'
    return LEGACY_SIZE[v] || v
  }
  const setRecSize = (rec, size) => setRowSizes((m) => ({ ...m, [recKey(rec)]: size }))
  // Queue/Create an accepted idea. The idea stays in the Accepted list — it's
  // only stamped as acted upon, so the list shows what's still waiting.
  const queueAccepted = async (rec) => {
    const { minutes, resolution } = presetFor(rec, recSize(rec))
    setBusy('acc-' + recKey(rec)); setError('')
    try {
      await api.queueAdd(rec.title, minutes, rec.reason || '', resolution, styleOf(rec), rec.source === 'news' ? rec.id || '' : '')
      await api.actSuggestion({ id: rec.id || '', title: rec.title || '', via: 'queue' }).catch(() => {})
      await loadAccepted()
      setStatus('Added to queue.')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }
  const createAccepted = async (rec) => {
    const { minutes, resolution } = presetFor(rec, recSize(rec))
    await api.actSuggestion({ id: rec.id || '', title: rec.title || '', via: 'create' }).catch(() => {})
    go('create', { title: rec.title, description: rec.reason || '', minutes, resolution, styleName: styleOf(rec), ideaId: rec.source === 'news' ? rec.id || '' : '' })
  }
  // Bring a declined idea back into the active list. Clear the local hide too,
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
  // Remove an idea from the Accepted/Declined lists for good — it may
  // resurface organically in a future generation.
  const forgetIdea = async (rec) => {
    setError('')
    setDiscarded((arr) => arr.filter((r) => !sameIdea(r, rec)))
    setAccepted((arr) => arr.filter((r) => !sameIdea(r, rec)))
    try {
      await api.forgetSuggestion({ id: rec.id || '', title: rec.title || '' })
      setStatus('Idea removed.')
    } catch (e) { setError(e.message); loadDiscarded(); loadAccepted() }
  }

  // Sort is view-only — it reorders the cards, it never drops an idea (ideas
  // leave only on Accept/Decline/Ignore). Predicted views read from the
  // per-card reach lookups; ideas with no prediction yet fall back to newest.
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

  const acceptedPending = accepted.filter((r) => !r.acted)
  const acceptedActed = accepted.filter((r) => r.acted)

  // One row in the Accepted list: pick a size, then Queue/Create it (stays
  // listed, moves to "Acted on"), move it to Declined, or remove it.
  const acceptedRow = (rec) => {
    const size = recSize(rec)
    const { minutes, resolution } = presetFor(rec, size)
    const k = recKey(rec)
    return (
      <div key={k} className="row center between row--wrap gap-10"
        style={{ padding: '10px 0', borderTop: '1px solid var(--line)' }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600 }}>
            {rec.title}
            {isAll && rec.style_name ? <span style={{ marginLeft: 8 }}><Chip>{rec.style_name}</Chip></span> : null}
            {rec.acted ? <span style={{ marginLeft: 8 }}><Chip tone="accent"><Icon name={rec.acted_via === 'create' ? 'wand-magic-sparkles' : 'layer-group'} style={{ fontSize: 10 }} /> {rec.acted_via === 'create' ? 'Sent to Create' : 'Queued'}</Chip></span> : null}
          </div>
          {rec.reason && <div className="muted" style={{ fontSize: 12.5, fontStyle: 'italic' }}>{rec.reason}</div>}
          <NewsDetails idea={rec} />
        </div>
        <div className="row center gap-10 row--wrap">
          <Segmented value={size} onChange={(v) => setRecSize(rec, v)}
            options={SIZE_ORDER.map((s) => ({ value: s, label: SIZE_LABELS[s] }))} />
          <span className="muted" style={{ fontSize: 12.5 }}>{fmtDuration(minutes)} · {orientationOf(resolution)}</span>
          <Button variant="ghost" icon="ban" disabled={busy === 'acc-' + k} onClick={() => moveIdea(rec, 'declined')} title="Move to the Declined list">Decline</Button>
          <Button variant="ghost" icon="trash-can" disabled={busy === 'acc-' + k} onClick={() => forgetIdea(rec)} title="Remove from this list — it may resurface later">Remove</Button>
          <Button variant="ghost" icon="layer-group" disabled={busy === 'acc-' + k} onClick={() => queueAccepted(rec)}>Queue</Button>
          <Button variant="primary" icon="wand-magic-sparkles" disabled={busy === 'acc-' + k} onClick={() => createAccepted(rec)}>Create</Button>
        </div>
      </div>
    )
  }

  return (
    <div>
      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone={isNews ? 'info' : 'ok'}>{status}</Banner>}

      <div className="bento">
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center between row--wrap gap-10">
            <div className="row center gap-10">
              <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name={isNews ? 'newspaper' : 'lightbulb'} /></span>
              <div><div style={{ fontWeight: 600 }}>{isNews ? 'News ideas' : 'Topic ideas'}</div><div className="muted" style={{ fontSize: 12.5 }}>{isNews ? 'Video and song ideas from the X posts your styles monitor. Review their sources, then accept, queue or create.' : "Accept the ideas you like, decline the ones you don't — accepted ideas wait under Accepted until you queue or create them."}</div></div>
            </div>
            <Segmented value={view} onChange={setView}
              options={[
                { value: 'ideas', label: `Ideas (${ideas.length})` },
                { value: 'accepted', label: `Accepted (${accepted.length})` },
                { value: 'declined', label: `Declined (${discarded.length})` },
              ]} />
          </div>
          <div className="row gap-10 center mt-16" style={{ flexWrap: 'wrap' }}>
            {styleList.length > 0 && (
              <select className="select" value={isAll ? ALL_STYLES : effectiveStyle} onChange={(e) => pickStyle(e.target.value)}
                disabled={loadingIdeas || checkingNews} style={{ maxWidth: 220 }} title={isNews ? 'Show news for this style' : 'Ideas are generated for this style'}>
                {styleList.length > 1 && <option value={ALL_STYLES}>{isNews ? 'All styles' : 'All styles (mix)'}</option>}
                {styleTreeOrder(styleList).map(({ style: s, depth }) => (
                  <option key={s.name} value={s.name}>
                    {'  '.repeat(depth)}{depth ? '↳ ' : ''}{s.name}{meta.config?.default_style === s.name ? ' (default)' : ''}
                  </option>
                ))}
              </select>
            )}
            {view === 'ideas' && (
              <>
                <select className="select" value={sortBy} onChange={(e) => setSortBy(e.target.value)}
                  style={{ maxWidth: 180 }} title="Sort the ideas">
                  {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                {!isNews && <>
                  <div className="grow">
                    <input className="input" placeholder="Guide the ideas — e.g. Rock bands of the 90s"
                      value={guidance} onChange={(e) => setGuidance(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter' && !loadingIdeas) loadIdeas(guidance, true) }} />
                  </div>
                  <Button variant="primary" icon="wand-magic-sparkles" disabled={loadingIdeas} onClick={() => loadIdeas(guidance, true)}>
                    {loadingIdeas ? 'Thinking…' : (guidance.trim() ? 'Generate ideas' : 'Generate more')}</Button>
                </>}
              </>
            )}
          </div>
        </Card>
        {isNews && <Card span={12} className="reveal reveal-d1">
          <div className="row center between row--wrap gap-10">
            <div>
              <div style={{ fontWeight: 600 }}>News monitor</div>
              <div className="muted" style={{ fontSize: 12.5 }}>Recent X posts become ideas in each enabled style. Configure topics and automation in Settings → Styles.</div>
            </div>
            <Button variant="ghost" icon="rotate" disabled={checkingNews || !newsStatus?.configured || !newsStatus?.styles?.some((s) => s.enabled)} onClick={checkNews}>
              {checkingNews ? 'Checking X…' : 'Check news now'}
            </Button>
          </div>
          {newsError && <Banner tone="danger">{newsError}</Banner>}
          {newsStatus && !newsStatus.configured && <div className="mt-16"><Banner tone="warn">{newsStatus.connection_error || 'Connect an X account in Settings → Channels → X to search for news.'}</Banner></div>}
          {newsStatus?.configured && <p className="muted" style={{ fontSize: 13 }}>
            {newsStatus.auth_source === 'account'
              ? `Searching with ${newsStatus.account_name ? `@${newsStatus.account_name}` : newsStatus.account}.`
              : 'Searching with the configured X bearer token.'}
          </p>}
          {newsStatus?.background_enabled === false && <p className="muted" style={{ fontSize: 13 }}>Manual checks only on this server. Scheduled monitoring is disabled.</p>}
          {newsStatus?.styles?.map((monitor) => (
            <div key={monitor.style_name} className="stack gap-8 mt-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 12 }}>
              <div className="row center gap-10 row--wrap">
                <strong>{monitor.style_name}</strong>
                <Chip tone={monitor.enabled ? 'accent' : undefined}>{monitor.enabled ? (newsStatus.background_enabled === false ? 'Manual checks only' : `Every ${monitor.interval_minutes} min`) : 'Off'}</Chip>
                {monitor.enabled && <span className="muted" style={{ fontSize: 12.5 }}>{monitor.auto_queue ? (monitor.auto_accept ? 'Auto-accept + queue' : 'Review then auto-queue') : monitor.auto_accept ? 'Auto-accept' : 'Review ideas'} · up to {monitor.max_ideas} ideas/check</span>}
              </div>
              {monitor.enabled && <>
                <div style={{ fontSize: 13 }}>{monitor.query || 'No search query configured.'}</div>
                {monitor.last_query && monitor.last_query !== monitor.query && <div className="muted" style={{ fontSize: 12 }}>Last check used a different query: {monitor.last_query}</div>}
                <div className="muted" style={{ fontSize: 12 }}>Last check: {newsTime(monitor.last_checked)} · Last successful check: {newsTime(monitor.last_success)}</div>
                {monitor.last_checked && <div style={{ fontSize: 13 }}>
                  {monitor.posts_fetched ?? '—'} posts fetched · {monitor.posts_new ?? '—'} new posts · {monitor.ideas_added || 0} ideas added
                  {newsOutcome(monitor) && <div className="muted">{newsOutcome(monitor)}</div>}
                </div>}
                {monitor.last_error && <Banner tone="danger">{monitor.last_error}</Banner>}
              </>}
            </div>
          ))}
        </Card>}
        {isNews && view === 'ideas' && ideas.length === 0 && <Card span={12}>
          <div style={{ fontWeight: 600 }}>{loadingIdeas ? 'Loading news ideas…' : 'No news ideas yet'}</div>
          {!loadingIdeas && <p className="muted" style={{ fontSize: 13 }}>Enable a style's news monitor in Settings → Styles, then use Check news now. Automatically accepted ideas appear in this News tab's Accepted view.</p>}
        </Card>}
        {view === 'ideas' && sortedIdeas.map((idea, i) => {
          const title = idea.title || idea.final_title || idea
          const size = ideaSize(idea)
          const { minutes, resolution } = presetFor(idea, size)
          const key = ideaKey(idea) || `${title}-${i}`
          const pk = ideaKey(idea)
          return (
            <Card key={key} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
              <div className="row center between">
                <span style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</span>
                <div className="row center gap-10">{isAll && idea.style_name && <Chip>{idea.style_name}</Chip>}<IdeaReach idea={idea} isShort={orientationOf(resolution) === 'Portrait'} onResult={(d) => setPreds((m) => ({ ...m, [pk]: d }))} /><Stars value={idea.interestingness} /></div>
              </div>
              {idea.reason && <p className="muted" style={{ fontSize: 13, margin: '10px 0 0', fontStyle: 'italic' }}>{idea.reason}</p>}
              <NewsDetails idea={idea} />
              <div className="row center mt-16">
                <Segmented value={size} onChange={(v) => setIdeaSize(idea, v)}
                  options={SIZE_ORDER.map((s) => ({ value: s, label: SIZE_LABELS[s] }))} />
              </div>
              <div className="row center between mt-16 row--wrap gap-10">
                <span className="muted" style={{ fontSize: 12.5 }}>{fmtDuration(minutes)} · {orientationOf(resolution)}</span>
                <div className="row gap-10 row--wrap">
                  <Button variant="ghost" icon="ban" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea, 'declined')} title="Add to the Declined list so the AI steers away from it">Decline</Button>
                  <Button variant="ghost" icon="eye-slash" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea, 'ignored')} title="Hide it for good — you won't see it again">Ignore</Button>
                  <Button variant="primary" icon="check" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea, 'accepted')} title="Keep it under Accepted, ready to queue or create">Accept</Button>
                </div>
              </div>
            </Card>
          )
        })}
        {view === 'accepted' && (
          <Card span={12} className="reveal">
            <div className="row center gap-10">
              <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="check" /></span>
              <div>
                <div style={{ fontWeight: 600 }}>Accepted ideas ({accepted.length})</div>
                <div className="muted" style={{ fontSize: 12.5 }}>{isNews ? 'News ideas' : 'Topics'} you accepted. Queue or create one — it stays here, marked as acted on, so you always know which ideas haven't been made yet.</div>
              </div>
            </div>
            {accepted.length === 0 && (
              <p className="muted" style={{ fontSize: 13, margin: '16px 0 0' }}>Nothing accepted yet — accept an idea from the Ideas tab and it will wait here.</p>
            )}
            {acceptedPending.length > 0 && (
              <div className="mt-16">
                <div className="label-sm">Not created yet ({acceptedPending.length})</div>
                {acceptedPending.map(acceptedRow)}
              </div>
            )}
            {acceptedActed.length > 0 && (
              <div className="mt-16">
                <div className="label-sm">Acted on ({acceptedActed.length})</div>
                {acceptedActed.map(acceptedRow)}
              </div>
            )}
          </Card>
        )}
        {view === 'declined' && (
          <Card span={12} className="reveal">
            <div className="row center gap-10">
              <span className="stream-ico" style={{ background: 'var(--surface-2)', color: 'var(--muted)' }}><Icon name="ban" /></span>
              <div>
                <div style={{ fontWeight: 600 }}>Declined ideas ({discarded.length})</div>
                <div className="muted" style={{ fontSize: 12.5 }}>{isNews ? 'News ideas' : 'Topics'} you turned down — kept out of new suggestions. Accept one to move it to the Accepted list, revive it back into the ideas, or remove it for good. (Ignored ideas stay hidden and aren't listed here.)</div>
              </div>
            </div>
            {discarded.length === 0 && (
              <p className="muted" style={{ fontSize: 13, margin: '16px 0 0' }}>Nothing declined right now.</p>
            )}
            {discarded.length > 0 && (
              <div className="mt-16">
                {discarded.map((rec) => (
                  <div key={rec.id || rec.title} className="row center between row--wrap gap-10"
                    style={{ padding: '10px 0', borderTop: '1px solid var(--line)' }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 600 }}>{rec.title}{isAll && rec.style_name ? <span style={{ marginLeft: 8 }}><Chip>{rec.style_name}</Chip></span> : null}</div>
                      {rec.reason && <div className="muted" style={{ fontSize: 12.5, fontStyle: 'italic' }}>{rec.reason}</div>}
                      <NewsDetails idea={rec} />
                    </div>
                    <div className="row gap-10">
                      <Button variant="ghost" icon="check" onClick={() => moveIdea(rec, 'accepted')} title="Move to the Accepted list">Accept</Button>
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
