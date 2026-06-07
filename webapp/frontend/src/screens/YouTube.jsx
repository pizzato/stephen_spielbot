import { useState, useEffect } from 'react'
import { Card, Chip, Button, Field, Segmented, Icon, Banner, composeResolution, resolutionTier } from '../components.jsx'
import { api } from '../api.js'
import Publish from './Publish.jsx'

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
function fmtDate(iso) {
  if (!iso) return ''
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso.slice(0, 10) }
}
function fmtDuration(secs) {
  if (secs == null) return '—'
  const s = Math.round(secs)
  const m = Math.floor(s / 60), rem = s % 60
  const h = Math.floor(m / 60), mins = m % 60
  if (h) return `${h}:${String(mins).padStart(2, '0')}:${String(rem).padStart(2, '0')}`
  return `${m}:${String(rem).padStart(2, '0')}`
}
function fmtWatchTime(mins) {
  if (mins == null) return '—'
  if (mins >= 1_000_000) return (mins / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M min'
  if (mins >= 1_000) return (mins / 1_000).toFixed(1).replace(/\.0$/, '') + 'K min'
  return mins + ' min'
}
function fmtPct(ratio) {
  if (ratio == null) return '—'
  return (ratio * 100).toFixed(1) + '%'
}
function StatCard({ label, value, sub, span = 3, delay = 1 }) {
  return (
    <Card span={span} className={`reveal reveal-d${delay}`}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.03em' }}>{value}</div>
      {sub && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{sub}</div>}
    </Card>
  )
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }

// Orientation follows the video length (short → portrait, else landscape); the
// pixel/quality tier follows the configured default so AI-idea and audience
// suggestions queue at the default resolution rather than a hardcoded 1080p.
const LENGTH_PRESETS = {
  short:  { scenes: 6,  orientation: 'Portrait' },
  medium: { scenes: 12, orientation: 'Landscape' },
  long:   { scenes: 20, orientation: 'Landscape' },
}

let _analyticsCache = null

const SEG_BADGE = { marginLeft: 6, background: 'var(--accent)', color: '#fff', fontSize: 10, fontWeight: 700, borderRadius: 999, padding: '1px 6px', minWidth: 16, display: 'inline-block', textAlign: 'center', lineHeight: '14px' }
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

export default function YouTube({ go, initial, meta = {} }) {
  const [view, setView] = useState(initial?.view || 'analytics')
  const [comments, setComments] = useState([])
  const [ideas, setIdeas] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [loadingIdeas, setLoadingIdeas] = useState(false)
  const [guidance, setGuidance] = useState('')
  const [videoLength, setVideoLength] = useState('medium')
  const [busy, setBusy] = useState('')          // action key currently running
  const [titles, setTitles] = useState({})      // per-comment edited title
  const [badges, setBadges] = useState({})      // {youtube_attention, youtube_publishable}
  const [analytics, setAnalytics] = useState(null)
  const [loadingAnalytics, setLoadingAnalytics] = useState(false)

  const refreshBadges = () => api.getBadges().then(setBadges).catch(() => {})
  const refreshComments = () => api.getComments().then((d) => { setComments(d.comments || []); refreshBadges() }).catch((e) => setError(e.message))

  useEffect(() => { refreshComments(); refreshBadges() }, [])

  // The text box steers generation (e.g. "Rock bands of the 90s" → ideas about
  // 90s rock bands); blank = general ideas from the channel's gaps.
  const loadIdeas = async (g = '', refresh = false) => {
    setLoadingIdeas(true); setError('')
    try { const d = await api.getSuggestions(g, refresh); setIdeas(visibleIdeas(d.suggestions || [])) }
    catch (e) { setError(e.message) } finally { setLoadingIdeas(false) }
  }
  // First visit loads the cached set (no LLM call); only regenerates if empty.
  useEffect(() => { if (view === 'ideas' && ideas.length === 0 && !loadingIdeas) loadIdeas('', false) }, [view])
  useEffect(() => { if (initial?.view) setView(initial.view) }, [initial])
  // Opening Publish marks the ready-to-publish videos as seen (mailbox-style),
  // so the "unread" count clears even if you don't actually publish.
  useEffect(() => { if (view === 'publish') api.markSeen('publish').then(refreshBadges).catch(() => {}) }, [view])

  const fetchEvaluate = async () => {
    setBusy('fetch'); setError(''); setStatus('')
    try {
      const r = await api.fetchComments()
      setComments(r.comments || [])
      refreshBadges()
      setStatus(`Fetched ${r.new} new · ${r.auto_approved} auto-approved · ${r.thanked} thanked`)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const approve = async (c) => {
    setBusy('a' + c.comment_id); setError('')
    try {
      const r = await api.approveComment(c.comment_id, titles[c.comment_id] ?? c.suggested_title)
      setStatus(`Approved → queued: ${r.final_title}`)
      await refreshComments()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const reject = async (c) => {
    setBusy('r' + c.comment_id); setError('')
    try { await api.rejectComment(c.comment_id); await refreshComments(); setStatus('Rejected.') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const reply = async (c) => {
    const text = window.prompt(`Reply to ${c.commenter}:`, '')
    if (!text) return
    setBusy('y' + c.comment_id); setError('')
    try { await api.replyComment(c.comment_id, text); setStatus('Reply posted.') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const pending = (c) => c.is_request && !['approved', 'rejected'].includes(c.status)
  const ideaKey = (idea) => idea?.id || idea?.title || idea?.final_title || ''
  const removeIdeaLocal = (idea) => {
    const key = ideaKey(idea)
    const title = idea?.title || idea?.final_title || idea
    setIdeas((arr) => arr.filter((it) => ideaKey(it) !== key && (it.title || it.final_title || it) !== title))
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
    const { scenes } = LENGTH_PRESETS[videoLength]
    const resolution = presetResolution(videoLength)
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
    const { scenes } = LENGTH_PRESETS[videoLength]
    const resolution = presetResolution(videoLength)
    await closeIdea(idea, 'used')
    go('create', { title, description: idea.reason || '', scenes, resolution })
  }

  const fetchAnalytics = async () => {
    setLoadingAnalytics(true); setError('')
    try { const d = await api.ytAnalytics(); _analyticsCache = d; setAnalytics(d) }
    catch (e) { setAnalytics({ channel: {}, videos: [], error: e.message }) } finally { setLoadingAnalytics(false) }
  }
  useEffect(() => {
    if (view !== 'analytics') return
    if (_analyticsCache && !analytics) { setAnalytics(_analyticsCache); return }
    if (!analytics && !loadingAnalytics) fetchAnalytics()
  }, [view])

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">YouTube</span>
          <h1 className="display-md reveal reveal-d1">Comments, ideas &amp; publishing</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          <Chip tone="ok" dot>@StephenSpielbot</Chip>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={setView} options={[
          { value: 'analytics', label: 'Analytics' },
          { value: 'comments', label: badges.youtube_attention ? <span>Comments <span style={SEG_BADGE}>{badges.youtube_attention}</span></span> : 'Comments' },
          { value: 'ideas', label: 'AI ideas' },
          { value: 'publish', label: badges.youtube_publishable ? <span>Publish <span style={SEG_BADGE}>{badges.youtube_publishable}</span></span> : 'Publish' },
        ]} />
      </div>

      {view === 'comments' && (
        <div className="bento">
          <Card span={12} well className="reveal reveal-d1">
            <div className="row center between">
              <span className="muted" style={{ fontSize: 13 }}>Pull the latest channel comments and rank video requests.</span>
              <Button variant="ghost" icon="rotate" disabled={busy === 'fetch'} onClick={fetchEvaluate}>
                {busy === 'fetch' ? 'Fetching…' : 'Fetch & evaluate'}
              </Button>
            </div>
          </Card>
          {comments.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No comments cached. Click <strong>Fetch &amp; evaluate</strong> to pull and rank the latest channel comments.</p></Card>}
          {comments.map((c, i) => (
            <Card key={c.comment_id || i} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
              <div className="row center between">
                <span style={{ fontWeight: 700 }}>{c.commenter || 'viewer'}</span>
                {c.is_request ? <Chip tone="ok"><Icon name="check" style={{ fontSize: 10 }} /> Request</Chip> : <Chip tone="neutral">Not a request</Chip>}
              </div>
              <p className="body-1" style={{ fontSize: 14, margin: '10px 0 0' }}>{c.text}</p>

              {c.is_request && (
                <div className="mt-16 stack gap-10">
                  <div className="row center gap-16" style={{ flexWrap: 'wrap' }}>
                    <Stars value={c.interestingness} />
                    {c.confidence != null && <span className="muted" style={{ fontSize: 12.5 }}>conf {Math.round(c.confidence * 100)}%</span>}
                    {c.suggested_scene_count ? <span className="muted" style={{ fontSize: 12.5 }}>{c.suggested_scene_count} scenes · {tier(c.suggested_scene_count)}</span> : null}
                    {c.status && c.status !== 'evaluated' && <Chip tone={c.status === 'approved' ? 'ok' : c.status === 'rejected' ? 'danger' : 'accent'}>{c.status}</Chip>}
                  </div>
                  {c.reason && <p className="muted" style={{ fontSize: 12.5, margin: 0, fontStyle: 'italic' }}>{c.reason}</p>}
                  {pending(c) && (
                    <>
                      <Field label="Title for the queue">
                        <input className="input" value={titles[c.comment_id] ?? c.suggested_title ?? ''}
                          onChange={(e) => setTitles((t) => ({ ...t, [c.comment_id]: e.target.value }))} />
                      </Field>
                      <div className="row gap-10 row--wrap">
                        <Button variant="primary" icon="plus" disabled={busy === 'a' + c.comment_id} onClick={() => approve(c)}>Approve → queue</Button>
                        <Button variant="ghost" icon="reply" disabled={busy === 'y' + c.comment_id} onClick={() => reply(c)}>Reply</Button>
                        <Button variant="danger" icon="xmark" disabled={busy === 'r' + c.comment_id} onClick={() => reject(c)}>Reject</Button>
                      </div>
                    </>
                  )}
                </div>
              )}
              {!c.is_request && (
                <div className="row gap-10 mt-16">
                  <Button variant="ghost" icon="reply" disabled={busy === 'y' + c.comment_id} onClick={() => reply(c)}>Reply</Button>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      {view === 'ideas' && (
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
            <div className="row center gap-10 mt-16" style={{ flexWrap: 'wrap' }}>
              <span className="muted" style={{ fontSize: 12.5 }}>Video length</span>
              <Segmented value={videoLength} onChange={setVideoLength} options={[
                { value: 'short',  label: `Short · ${LENGTH_PRESETS.short.scenes} scenes · portrait` },
                { value: 'medium', label: `Medium · ${LENGTH_PRESETS.medium.scenes} scenes · landscape` },
                { value: 'long',   label: `Long · ${LENGTH_PRESETS.long.scenes} scenes · landscape` },
              ]} />
            </div>
          </Card>
          {ideas.map((idea, i) => {
            const title = idea.title || idea.final_title || idea
            const { scenes: presetScenes } = LENGTH_PRESETS[videoLength]
            const key = ideaKey(idea) || `${title}-${i}`
            return (
              <Card key={key} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
                <div className="row center between">
                  <span style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</span>
                  <Stars value={idea.interestingness} />
                </div>
                {idea.reason && <p className="muted" style={{ fontSize: 13, margin: '10px 0 0', fontStyle: 'italic' }}>{idea.reason}</p>}
                <div className="row center between mt-16 row--wrap gap-10">
                  <span className="muted" style={{ fontSize: 12.5 }}>{presetScenes} scenes · {tier(presetScenes)}</span>
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
      )}

      {view === 'publish' && <Publish initialWorkDir={initial?.workDir} go={go} />}

      {view === 'analytics' && (
        <div className="bento">
          {loadingAnalytics && (
            <Card span={12}><p className="muted" style={{ fontSize: 13 }}>Loading analytics…</p></Card>
          )}
          {!loadingAnalytics && !analytics && (
            <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No data yet. Connect YouTube to see your channel analytics.</p></Card>
          )}
          {analytics?.error && (
            <Card span={12}><p style={{ fontSize: 13, color: 'var(--danger)' }}>{analytics.error}</p></Card>
          )}
          {!loadingAnalytics && analytics && !analytics.error && (() => {
            const ch = analytics.channel || {}
            const dailyViews = ch.daily_views || []
            const maxDailyViews = Math.max(1, ...dailyViews.map(d => d.views))
            const trafficSources = ch.traffic_sources || []
            const topCountries = ch.top_countries || []
            return (
            <>
              {/* Row 1 — totals */}
              <StatCard label="Subscribers" value={fmtNum(ch.subscriber_count)} delay={1} />
              <StatCard label="Total views" value={fmtNum(ch.view_count)} delay={2} />
              <StatCard label="Watch time" value={fmtWatchTime(ch.watch_time_minutes)} delay={3} />
              <StatCard label="Videos" value={fmtNum(ch.video_count)}
                sub={<Button variant="ghost" icon="rotate" style={{ marginTop: 8 }} disabled={loadingAnalytics} onClick={fetchAnalytics}>Refresh</Button>}
                delay={4} />

              {/* Row 2 — engagement */}
              <StatCard label="Avg view duration" value={ch.avg_view_duration_secs != null ? fmtDuration(ch.avg_view_duration_secs) : '—'} delay={1} />
              <StatCard label="Impressions" value={fmtNum(ch.impressions)} delay={2} />
              <StatCard label="Click-through rate" value={ch.ctr != null ? fmtPct(ch.ctr) : '—'} delay={3} />
              <StatCard label="Subs gained" value={ch.subscribers_gained != null ? fmtNum(ch.subscribers_gained) : '—'}
                sub={ch.subscribers_lost != null ? `−${fmtNum(ch.subscribers_lost)} lost` : null} delay={4} />

              {/* Daily views chart */}
              {dailyViews.length > 0 && (
                <Card span={12} className="reveal reveal-d1">
                  <div style={{ fontWeight: 600, marginBottom: 12 }}>
                    Views — last 28 days
                    <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 10 }}>
                      {fmtNum(dailyViews.reduce((s, d) => s + d.views, 0))} total
                    </span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 72 }}>
                    {dailyViews.map(d => (
                      <div key={d.date} title={`${d.date}: ${d.views.toLocaleString()} views`} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, height: '100%', justifyContent: 'flex-end' }}>
                        <div style={{ width: '100%', background: 'var(--accent)', borderRadius: '3px 3px 0 0', height: `${Math.max(2, Math.round(d.views / maxDailyViews * 64))}px`, opacity: 0.85 }} />
                      </div>
                    ))}
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6 }}>
                    <span className="muted" style={{ fontSize: 11 }}>{dailyViews[0]?.date?.slice(5)}</span>
                    <span className="muted" style={{ fontSize: 11 }}>{dailyViews[dailyViews.length - 1]?.date?.slice(5)}</span>
                  </div>
                </Card>
              )}

              {/* Traffic sources + Top countries */}
              {trafficSources.length > 0 && (
                <Card span={6} className="reveal reveal-d1">
                  <div style={{ fontWeight: 600, marginBottom: 14 }}>Traffic sources</div>
                  <div className="stack" style={{ gap: 10 }}>
                    {trafficSources.map(ts => (
                      <div key={ts.source}>
                        <div className="row center between" style={{ fontSize: 13, marginBottom: 4 }}>
                          <span>{ts.label}</span>
                          <span className="muted">{fmtNum(ts.views)} <span style={{ fontSize: 11 }}>({ts.pct}%)</span></span>
                        </div>
                        <div style={{ height: 4, background: 'var(--border)', borderRadius: 2 }}>
                          <div style={{ height: '100%', width: `${ts.pct}%`, background: 'var(--accent)', borderRadius: 2 }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </Card>
              )}
              {topCountries.length > 0 && (
                <Card span={topCountries.length > 0 && trafficSources.length > 0 ? 6 : 12} className="reveal reveal-d2">
                  <div style={{ fontWeight: 600, marginBottom: 14 }}>Top countries</div>
                  <div className="stack" style={{ gap: 10 }}>
                    {topCountries.map(c => (
                      <div key={c.country}>
                        <div className="row center between" style={{ fontSize: 13, marginBottom: 4 }}>
                          <span style={{ fontWeight: 500 }}>{c.country}</span>
                          <span className="muted">{fmtNum(c.views)} <span style={{ fontSize: 11 }}>({c.pct}%)</span></span>
                        </div>
                        <div style={{ height: 4, background: 'var(--border)', borderRadius: 2 }}>
                          <div style={{ height: '100%', width: `${c.pct}%`, background: 'var(--accent)', borderRadius: 2, opacity: 0.7 }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </Card>
              )}

              {/* Per-video table */}
              {(analytics.videos || []).length > 0 && (
                <Card span={12} className="reveal reveal-d2">
                  <div style={{ fontWeight: 600, marginBottom: 14 }}>Recent videos</div>
                  <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                      <thead>
                        <tr style={{ borderBottom: '1px solid var(--border)' }}>
                          {['', 'Title', 'Duration', 'Views', 'Watch time', 'Avg duration', 'Retention', 'Impressions', 'CTR', 'Likes', 'Published'].map((h, i) => (
                            <th key={i} style={{ textAlign: i <= 1 ? 'left' : 'right', padding: i === 0 ? '6px 10px 6px 0' : i === 10 ? '6px 0 6px 10px' : '6px 10px', fontWeight: 600, color: 'var(--muted)', whiteSpace: 'nowrap', width: i === 0 ? 52 : undefined }}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {analytics.videos.map((v) => (
                          <tr key={v.video_id} style={{ borderBottom: '1px solid var(--border-subtle, var(--border))' }}>
                            <td style={{ padding: '8px 10px 8px 0' }}>
                              {v.thumbnail_url
                                ? <img src={v.thumbnail_url} alt="" style={{ width: 48, height: 27, objectFit: 'cover', borderRadius: 4, display: 'block' }} />
                                : <div style={{ width: 48, height: 27, background: 'var(--surface-2)', borderRadius: 4 }} />}
                            </td>
                            <td style={{ padding: '8px 10px' }}>
                              <div className="row center gap-10" style={{ flexWrap: 'nowrap' }}>
                                <a href={`https://www.youtube.com/watch?v=${v.video_id}`} target="_blank" rel="noreferrer"
                                  style={{ color: 'inherit', textDecoration: 'none', fontWeight: 500 }}>{v.title}</a>
                                {v.privacy && v.privacy !== 'public' && (
                                  <span style={{ flexShrink: 0, fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.04em', background: 'var(--surface-2)', borderRadius: 4, padding: '2px 6px', color: 'var(--muted)' }}>{v.privacy}</span>
                                )}
                              </div>
                            </td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{v.duration || '—'}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', fontWeight: 600 }}>{fmtNum(v.view_count)}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{fmtWatchTime(v.watch_time_minutes)}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{fmtDuration(v.avg_view_duration_secs)}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{v.avg_view_pct != null ? `${v.avg_view_pct}%` : '—'}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{fmtNum(v.impressions)}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{v.ctr != null ? fmtPct(v.ctr) : '—'}</td>
                            <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{fmtNum(v.like_count)}</td>
                            <td style={{ textAlign: 'right', padding: '8px 0 8px 10px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{fmtDate(v.published_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
            </>
            )
          })()}
        </div>
      )}
    </div>
  )
}
