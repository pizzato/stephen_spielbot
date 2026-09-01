import { useState, useEffect } from 'react'
import { Card, Chip, Button, Segmented, Banner } from '../components.jsx'
import { api } from '../api.js'

// Unified "Analytics" screen (issue #107): the YouTube dashboard and the X
// dashboard behind a platform toggle. The two are structurally different
// (YouTube has watch-time/CTR/traffic; X has impressions/likes/reposts), so each
// has its own render block — they just share this page, the toggle, and the
// channel/account selector.

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
  const s = Math.round(secs), m = Math.floor(s / 60), rem = s % 60, h = Math.floor(m / 60), mins = m % 60
  if (h) return `${h}:${String(mins).padStart(2, '0')}:${String(rem).padStart(2, '0')}`
  return `${m}:${String(rem).padStart(2, '0')}`
}
function fmtWatchTime(mins) {
  if (mins == null) return '—'
  if (mins >= 1_000_000) return (mins / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M min'
  if (mins >= 1_000) return (mins / 1_000).toFixed(1).replace(/\.0$/, '') + 'K min'
  return mins + ' min'
}
function fmtPct(ratio) { return ratio == null ? '—' : (ratio * 100).toFixed(1) + '%' }

function StatCard({ label, value, sub, span = 3, delay = 1 }) {
  return (
    <Card span={span} className={`reveal reveal-d${delay}`}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.03em' }}>{value}</div>
      {sub && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{sub}</div>}
    </Card>
  )
}

const _cache = { youtube: {}, x: {} }   // per-platform, per-target snapshot cache

// Recent-videos table columns. `key` makes a header clickable; `val` overrides
// the sort value (derived metrics). Dislike % = dislikes / (likes + dislikes),
// the "how hated" measure — raw dislikes just tracks reach.
const dislikePct = (v) => {
  if (v.dislike_count == null) return null
  const total = (v.like_count || 0) + v.dislike_count
  return total ? v.dislike_count / total : null
}
// Composite hate score 0–100, computed for EVERY video:
//   45% dislike share + 30% negative-comment share + 25% unpopularity.
// The dislike/comment shares are normalised by audience size — the pseudo-counts
// scale with views (roughly the reactions ~2% and comments ~1% of viewers leave),
// so 2 dislikes on 20 views scores real points while the same 2 dislikes on 1M
// views round to zero. Unpopularity is reach measured against the channel's
// own catalogue, age-adjusted: views grow sub-linearly with age (a burst early,
// then a long tail), so each video's views are normalised by √days-since-publish
// before comparing to the channel median — an old 50-view video ranks worse
// than a day-old one at the same count. A 3-day grace period fades the term in
// from zero, so a just-posted video can't top the hate list merely because
// views haven't accumulated yet (dislikes and negative comments still count
// from day one). Unfetched dislikes count as zero rather than hiding the score.
function annotateHateScores(videos) {
  const days = (v) => Math.max(1, (Date.now() - new Date(v.published_at).getTime()) / 86400000)
  const reach = (v) => (v.view_count || 0) / Math.sqrt(days(v))
  const reaches = videos.map(reach).sort((a, b) => a - b)
  const median = reaches.length ? reaches[Math.floor(reaches.length / 2)] : 0
  return videos.map((v) => {
    const d = v.dislike_count || 0, l = v.like_count || 0, views = v.view_count || 0
    const reactions = d / (l + d + Math.max(10, 0.02 * views))
    const negComments = (v.negative_comment_count || 0) / ((v.comment_count || 0) + Math.max(5, 0.01 * views))
    const unpopular = (median > 0 ? median / (reach(v) + median) : 0.5) * Math.min(1, days(v) / 3)
    return { ...v, hate_score: Math.round(100 * (0.45 * reactions + 0.3 * negComments + 0.25 * unpopular)) }
  })
}
const VIDEO_COLS = [
  { label: '' },
  { label: 'Title', align: 'left' },
  { label: 'Duration' },
  { label: 'Views', key: 'view_count' },
  { label: 'Watch time', key: 'watch_time_minutes' },
  { label: 'Avg duration', key: 'avg_view_duration_secs' },
  { label: 'Retention', key: 'avg_view_pct' },
  { label: 'Impressions', key: 'impressions' },
  { label: 'CTR', key: 'ctr' },
  { label: 'Likes', key: 'like_count' },
  { label: 'Dislikes', key: 'dislike_count' },
  { label: 'Dislike %', key: 'dislike_pct', val: dislikePct },
  { label: 'Comments', key: 'comment_count' },
  { label: 'Negative', key: 'negative_comment_count' },
  { label: 'Hate score', key: 'hate_score' },
  { label: 'Published', key: 'published_at' },
]

function sortVideos(videos, sort) {
  const col = VIDEO_COLS.find((c) => c.key === sort.key)
  if (!col) return videos
  const val = (v) => {
    const x = col.val ? col.val(v) : v[sort.key]
    return x == null ? -1 : x   // missing metrics sink to the bottom on desc
  }
  const dir = sort.dir === 'asc' ? 1 : -1
  return [...videos].sort((a, b) => { const av = val(a), bv = val(b); return (av < bv ? -1 : av > bv ? 1 : 0) * dir })
}

function YouTubeAnalytics({ analytics, loading, onRefresh }) {
  const [sort, setSort] = useState({ key: null, dir: 'desc' })
  if (loading) return <Card span={12}><p className="muted" style={{ fontSize: 13 }}>Loading analytics…</p></Card>
  if (!analytics) return <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No data yet. Connect YouTube to see your channel analytics.</p></Card>
  if (analytics.error) return <Card span={12}><p style={{ fontSize: 13, color: 'var(--danger)' }}>{analytics.error}</p></Card>
  const ch = analytics.channel || {}
  const dailyViews = ch.daily_views || []
  const maxDaily = Math.max(1, ...dailyViews.map((d) => d.views))
  const traffic = ch.traffic_sources || []
  const countries = ch.top_countries || []
  return (
    <>
      <StatCard label="Subscribers" value={fmtNum(ch.subscriber_count)} delay={1} />
      <StatCard label="Total views" value={fmtNum(ch.view_count)} delay={2} />
      <StatCard label="Watch time" value={fmtWatchTime(ch.watch_time_minutes)} delay={3} />
      <StatCard label="Videos" value={fmtNum(ch.video_count)}
        sub={<Button variant="ghost" icon="rotate" style={{ marginTop: 8 }} disabled={loading} onClick={onRefresh}>Refresh</Button>} delay={4} />
      <StatCard label="Avg view duration" value={ch.avg_view_duration_secs != null ? fmtDuration(ch.avg_view_duration_secs) : '—'} delay={1} />
      <StatCard label="Impressions" value={fmtNum(ch.impressions)} delay={2} />
      <StatCard label="Click-through rate" value={ch.ctr != null ? fmtPct(ch.ctr) : '—'} delay={3} />
      <StatCard label="Subs gained" value={ch.subscribers_gained != null ? fmtNum(ch.subscribers_gained) : '—'}
        sub={ch.subscribers_lost != null ? `−${fmtNum(ch.subscribers_lost)} lost` : null} delay={4} />
      {dailyViews.length > 0 && (
        <Card span={12} className="reveal reveal-d1">
          <div style={{ fontWeight: 600, marginBottom: 12 }}>Views — last 28 days
            <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 10 }}>{fmtNum(dailyViews.reduce((s, d) => s + d.views, 0))} total</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 72 }}>
            {dailyViews.map((d) => (
              <div key={d.date} title={`${d.date}: ${d.views.toLocaleString()} views`} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, height: '100%', justifyContent: 'flex-end' }}>
                <div style={{ width: '100%', background: 'var(--accent)', borderRadius: '3px 3px 0 0', height: `${Math.max(2, Math.round(d.views / maxDaily * 64))}px`, opacity: 0.85 }} />
              </div>
            ))}
          </div>
        </Card>
      )}
      {traffic.length > 0 && (
        <Card span={6} className="reveal reveal-d1">
          <div style={{ fontWeight: 600, marginBottom: 14 }}>Traffic sources</div>
          <div className="stack" style={{ gap: 10 }}>
            {traffic.map((ts) => (
              <div key={ts.source}>
                <div className="row center between" style={{ fontSize: 13, marginBottom: 4 }}><span>{ts.label}</span><span className="muted">{fmtNum(ts.views)} <span style={{ fontSize: 11 }}>({ts.pct}%)</span></span></div>
                <div style={{ height: 4, background: 'var(--line)', borderRadius: 2 }}><div style={{ height: '100%', width: `${ts.pct}%`, background: 'var(--accent)', borderRadius: 2 }} /></div>
              </div>
            ))}
          </div>
        </Card>
      )}
      {countries.length > 0 && (
        <Card span={traffic.length > 0 ? 6 : 12} className="reveal reveal-d2">
          <div style={{ fontWeight: 600, marginBottom: 14 }}>Top countries</div>
          <div className="stack" style={{ gap: 10 }}>
            {countries.map((c) => (
              <div key={c.country}>
                <div className="row center between" style={{ fontSize: 13, marginBottom: 4 }}><span style={{ fontWeight: 500 }}>{c.country}</span><span className="muted">{fmtNum(c.views)} <span style={{ fontSize: 11 }}>({c.pct}%)</span></span></div>
                <div style={{ height: 4, background: 'var(--line)', borderRadius: 2 }}><div style={{ height: '100%', width: `${c.pct}%`, background: 'var(--accent)', borderRadius: 2, opacity: 0.7 }} /></div>
              </div>
            ))}
          </div>
        </Card>
      )}
      {(analytics.videos || []).length > 0 && (
        // minWidth: 0 lets the inner overflow-x scroller work — without it the
        // grid item grows to fit the table and the right-hand columns clip.
        <Card span={12} className="reveal reveal-d2" style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>All videos
            <span className="muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 10 }}>{analytics.videos.length}</span>
          </div>
          <div className="muted" style={{ fontSize: 11, marginBottom: 12 }}>
            Click a column to sort — Hate score blends dislike share, negative comments and unpopularity (views/day vs the channel median) into one most-hated ranking.
            Negative counts LLM-classified fetched comments, so it's a floor, not a total.
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', minWidth: 1080, borderCollapse: 'collapse', fontSize: 13 }}>
              <thead><tr style={{ borderBottom: '1px solid var(--line)' }}>
                {VIDEO_COLS.map((c, i) => (
                  <th key={i}
                    onClick={c.key ? () => setSort((s) => ({ key: c.key, dir: s.key === c.key && s.dir === 'desc' ? 'asc' : 'desc' })) : undefined}
                    style={{ textAlign: c.align || (i <= 1 ? 'left' : 'right'), padding: '6px 10px', fontWeight: 600,
                             color: sort.key === c.key ? 'var(--ink)' : 'var(--ink-3)', whiteSpace: 'nowrap',
                             cursor: c.key ? 'pointer' : 'default', userSelect: 'none' }}>
                    {c.label}{sort.key === c.key ? (sort.dir === 'desc' ? ' ↓' : ' ↑') : ''}
                  </th>
                ))}
              </tr></thead>
              <tbody>
                {sortVideos(annotateHateScores(analytics.videos), sort).map((v) => {
                  const pct = dislikePct(v)
                  const score = v.hate_score
                  return (
                  <tr key={v.video_id} style={{ borderBottom: '1px solid var(--line)' }}>
                    <td style={{ padding: '8px 10px 8px 0' }}>{v.thumbnail_url ? <img src={v.thumbnail_url} alt="" style={{ width: 48, height: 27, objectFit: 'cover', borderRadius: 4, display: 'block' }} /> : <div style={{ width: 48, height: 27, background: 'var(--surface-2)', borderRadius: 4 }} />}</td>
                    <td style={{ padding: '8px 10px', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={v.title}><a href={`https://www.youtube.com/watch?v=${v.video_id}`} target="_blank" rel="noreferrer" style={{ color: 'inherit', textDecoration: 'none', fontWeight: 500 }}>{v.title}</a></td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{v.duration || '—'}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', fontWeight: 600 }}>{fmtNum(v.view_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{fmtWatchTime(v.watch_time_minutes)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{fmtDuration(v.avg_view_duration_secs)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{v.avg_view_pct != null ? `${v.avg_view_pct}%` : '—'}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.impressions)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{v.ctr != null ? fmtPct(v.ctr) : '—'}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.like_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: v.dislike_count ? 'var(--danger)' : 'var(--ink-3)' }}>{fmtNum(v.dislike_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: pct ? 'var(--danger)' : 'var(--ink-3)' }}>{pct != null ? fmtPct(pct) : '—'}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.comment_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: v.negative_comment_count ? 'var(--danger)' : 'var(--ink-3)' }}>{fmtNum(v.negative_comment_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', fontWeight: 600, color: score ? 'var(--danger)' : 'var(--ink-3)' }}>{score != null ? score : '—'}</td>
                    <td style={{ textAlign: 'right', padding: '8px 0 8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{fmtDate(v.published_at)}</td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  )
}

function XAnalytics({ analytics, loading, onRefresh }) {
  if (loading) return <Card span={12}><p className="muted" style={{ fontSize: 13 }}>Loading analytics…</p></Card>
  if (!analytics) return <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No data yet. Connect X to see your account analytics.</p></Card>
  if (analytics.error) return <Card span={12}><p style={{ fontSize: 13, color: 'var(--danger)' }}>{analytics.error}</p></Card>
  const ch = analytics.channel || {}
  const vids = analytics.videos || []
  return (
    <>
      <StatCard label="Followers" value={fmtNum(ch.followers_count)} delay={1} />
      <StatCard label="Posts" value={fmtNum(ch.tweet_count)} delay={2} />
      <StatCard label="Impressions (recent)" value={fmtNum(ch.impressions)} delay={3} />
      <StatCard label="Likes (recent)" value={fmtNum(ch.likes)}
        sub={<Button variant="ghost" icon="rotate" style={{ marginTop: 8 }} disabled={loading} onClick={onRefresh}>Refresh</Button>} delay={4} />
      {vids.length === 0 && (
        <Card span={12} className="reveal reveal-d1"><p className="muted" style={{ fontSize: 13 }}>No per-post metrics. Reading posts and engagement needs a paid X API tier — followers/post counts show above once connected.</p></Card>
      )}
      {vids.length > 0 && (
        <Card span={12} className="reveal reveal-d2">
          <div style={{ fontWeight: 600, marginBottom: 14 }}>Recent posts</div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', minWidth: 560, borderCollapse: 'collapse', fontSize: 13 }}>
              <thead><tr style={{ borderBottom: '1px solid var(--line)' }}>
                {['Post', 'Impressions', 'Likes', 'Reposts', 'Replies', 'Posted'].map((h, i) => (
                  <th key={i} style={{ textAlign: i === 0 ? 'left' : 'right', padding: '6px 10px', fontWeight: 600, color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{h}</th>
                ))}
              </tr></thead>
              <tbody>
                {vids.map((v) => (
                  <tr key={v.tweet_id} style={{ borderBottom: '1px solid var(--line)' }}>
                    <td style={{ padding: '8px 10px', maxWidth: 360 }}><a href={v.url} target="_blank" rel="noreferrer" style={{ color: 'inherit', textDecoration: 'none', fontWeight: 500 }}>{v.text || '(no text)'}</a></td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', fontWeight: 600 }}>{fmtNum(v.impression_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.like_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.retweet_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.reply_count)}</td>
                    <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{fmtDate(v.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  )
}

export default function Analytics() {
  const [platform, setPlatform] = useState('youtube')
  const [targets, setTargets] = useState([])
  const [target, setTarget] = useState('')
  const [analytics, setAnalytics] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // Load the channel/account list when the platform changes.
  useEffect(() => {
    setAnalytics(null); setTarget(''); setError('')
    const listApi = platform === 'youtube'
      ? () => api.ytChannels().then((r) => r.channels || [])
      : () => api.xAccounts().then((r) => r.accounts || [])
    listApi().then((list) => { setTargets(list); setTarget(list[0]?.id || '') }).catch(() => setTargets([]))
  }, [platform])

  const fetchAnalytics = async (refresh = false) => {
    setLoading(true); setError('')
    const call = platform === 'youtube' ? api.ytAnalytics : api.xAnalytics
    try { const d = await call(target, refresh); _cache[platform][target] = d; setAnalytics(d) }
    catch (e) { setAnalytics({ channel: {}, videos: [], error: e.message }) } finally { setLoading(false) }
  }
  useEffect(() => {
    if (target === '' && targets.length) return   // wait for the default target
    if (_cache[platform][target]) { setAnalytics(_cache[platform][target]); return }
    if (!loading) fetchAnalytics(false)
  }, [platform, target, targets])

  const prefix = platform === 'x' ? '@' : ''
  return (
    <div>
      <div className="row center reveal reveal-d1" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
        <Segmented value={platform} onChange={setPlatform} options={[
          { value: 'youtube', label: 'YouTube' },
          { value: 'x', label: 'X' },
        ]} />
        {targets.length > 1 && (
          <select className="select" value={target} onChange={(e) => setTarget(e.target.value)} style={{ maxWidth: 220 }}>
            {targets.map((t) => <option key={t.id} value={t.id}>{prefix}{t.name || t.id}</option>)}
          </select>
        )}
        {targets.length === 1 && <Chip tone="ok" dot>{prefix}{targets[0].name || targets[0].id}</Chip>}
      </div>

      {error && <Banner tone="danger">{error}</Banner>}

      <div className="bento">
        {platform === 'youtube'
          ? <YouTubeAnalytics analytics={analytics} loading={loading} onRefresh={() => fetchAnalytics(true)} />
          : <XAnalytics analytics={analytics} loading={loading} onRefresh={() => fetchAnalytics(true)} />}
      </div>
    </div>
  )
}
