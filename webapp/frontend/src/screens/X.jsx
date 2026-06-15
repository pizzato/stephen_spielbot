import { useState, useEffect } from 'react'
import { Card, Chip, Button, Field, Segmented, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'
import Publish from './Publish.jsx'

// X (Twitter) screen (issue #107). Mentions are the "comments"; requests feed the
// same generation queue as YouTube. Deliberately a focused sibling of YouTube.jsx
// rather than a refactor of it — the comment-card structure is shared in spirit,
// but X has no Analytics columns yet and the working YouTube screen is untested.

function Stars({ value }) {
  if (value == null) return null
  return <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(value).toFixed(1)}</span>
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }
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
function StatCard({ label, value, sub, delay = 1 }) {
  return (
    <Card span={3} className={`reveal reveal-d${delay}`}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.03em' }}>{value}</div>
      {sub && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{sub}</div>}
    </Card>
  )
}

let _xAnalyticsCache = {}   // per-account cache: {accountKey: data}

// Plain manual-reply composer for a mention (no AI draft button — engagement
// drafts already provide AI replies for non-request mentions).
function ReplyComposer({ comment, onSent, onCancel }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const send = async () => {
    const t = text.trim()
    if (!t) return
    setBusy(true); setError('')
    try { await api.xReplyComment(comment.comment_id, t); onSent() }
    catch (e) { setError(e.message); setBusy(false) }
  }
  return (
    <div className="stack gap-10 mt-10">
      <Field label="Your reply">
        <textarea className="input" rows={3} value={text} placeholder={`Reply to ${comment.commenter || 'user'}…`}
          onChange={(e) => setText(e.target.value)} />
      </Field>
      {error && <p style={{ fontSize: 12, color: 'var(--danger)', margin: 0 }}>{error}</p>}
      <div className="row gap-10 row--wrap">
        <Button variant="primary" icon="reply" disabled={busy || !text.trim()} onClick={send}>
          {busy ? 'Sending…' : 'Send reply'}</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  )
}

export default function X({ go, initial }) {
  const [view, setView] = useState(initial?.view || 'analytics')
  const [comments, setComments] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [titles, setTitles] = useState({})
  const [drafts, setDrafts] = useState({})
  const [replyFor, setReplyFor] = useState('')
  const [accounts, setAccounts] = useState(null)
  const [account, setAccount] = useState('')
  const [analytics, setAnalytics] = useState(null)
  const [loadingAnalytics, setLoadingAnalytics] = useState(false)

  const refreshComments = () => api.xComments().then((d) => setComments(d.comments || [])).catch((e) => setError(e.message))

  useEffect(() => {
    refreshComments()
    api.xAccounts().then((r) => {
      const list = r.accounts || []
      setAccounts(list)
      setAccount((a) => a || list[0]?.id || '')
    }).catch(() => setAccounts([]))
  }, [])
  useEffect(() => { if (initial?.view) setView(initial.view) }, [initial])

  const accountName = (key) => {
    const a = (accounts || []).find((x) => x.id === key)
    return a ? (a.name ? `@${a.name}` : a.id) : ''
  }

  const fetchAnalytics = async (refresh = false) => {
    setLoadingAnalytics(true); setError('')
    try { const d = await api.xAnalytics(account, refresh); _xAnalyticsCache[account] = d; setAnalytics(d) }
    catch (e) { setAnalytics({ channel: {}, videos: [], error: e.message }) } finally { setLoadingAnalytics(false) }
  }
  useEffect(() => {
    if (view !== 'analytics' || accounts === null) return
    if (_xAnalyticsCache[account]) { setAnalytics(_xAnalyticsCache[account]); return }
    if (!loadingAnalytics) fetchAnalytics(false)
  }, [view, account, accounts])

  const fetchEvaluate = async () => {
    setBusy('fetch'); setError(''); setStatus('')
    try {
      const r = await api.xFetchComments()
      setComments(r.comments || [])
      setStatus(`Fetched ${r.new} new · ${r.auto_approved} auto-approved · ${r.thanked} thanked · ${r.community_drafted ?? 0} drafted · ${r.community_sent ?? 0} sent`)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const approve = async (c) => {
    setBusy('a' + c.comment_id); setError('')
    try {
      const r = await api.xApproveComment(c.comment_id, titles[c.comment_id] ?? c.suggested_title)
      setStatus(`Approved → queued: ${r.final_title}`)
      await refreshComments()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const reject = async (c) => {
    setBusy('r' + c.comment_id); setError('')
    try { await api.xRejectComment(c.comment_id); await refreshComments(); setStatus('Rejected.') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const sendDraft = async (c) => {
    const text = (drafts[c.comment_id] ?? c.engagement_draft ?? '').trim()
    if (!text) return
    setBusy('cs' + c.comment_id); setError('')
    try { await api.xSendCommunityReply(c.comment_id, text); setStatus('Reply sent.'); await refreshComments() }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const dismissDraft = async (c) => {
    setBusy('cd' + c.comment_id); setError('')
    try { await api.xDismissCommunityReply(c.comment_id); await refreshComments(); setStatus('Draft dismissed.') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const pending = (c) => c.is_request && !['approved', 'rejected'].includes(c.status)
  const multi = (accounts || []).length > 1

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">X</span>
          <h1 className="display-md reveal reveal-d1">Mentions &amp; publishing</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          {(accounts || []).length > 1 ? (
            <select className="select" value={account} onChange={(e) => setAccount(e.target.value)} style={{ maxWidth: 220 }}>
              {(accounts || []).map((a) => <option key={a.id} value={a.id}>{a.name ? `@${a.name}` : a.id}</option>)}
            </select>
          ) : (accounts || []).length > 0 ? (
            <Chip tone="ok" dot>{accountName((accounts || [])[0]?.id) || 'X'}</Chip>
          ) : null}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={setView} options={[
          { value: 'analytics', label: 'Analytics' },
          { value: 'comments', label: 'Mentions' },
          { value: 'publish', label: 'Publish' },
        ]} />
      </div>

      {view === 'analytics' && (
        <div className="bento">
          {loadingAnalytics && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>Loading analytics…</p></Card>}
          {!loadingAnalytics && !analytics && (
            <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No data yet. Connect X to see your account analytics.</p></Card>
          )}
          {analytics?.error && (
            <Card span={12}><p style={{ fontSize: 13, color: 'var(--danger)' }}>{analytics.error}</p></Card>
          )}
          {!loadingAnalytics && analytics && !analytics.error && (() => {
            const ch = analytics.channel || {}
            const vids = analytics.videos || []
            return (
              <>
                <StatCard label="Followers" value={fmtNum(ch.followers_count)} delay={1} />
                <StatCard label="Posts" value={fmtNum(ch.tweet_count)} delay={2} />
                <StatCard label="Impressions (recent)" value={fmtNum(ch.impressions)} delay={3} />
                <StatCard label="Likes (recent)" value={fmtNum(ch.likes)}
                  sub={<Button variant="ghost" icon="rotate" style={{ marginTop: 8 }} disabled={loadingAnalytics} onClick={() => fetchAnalytics(true)}>Refresh</Button>}
                  delay={4} />
                {vids.length === 0 && (
                  <Card span={12} className="reveal reveal-d1">
                    <p className="muted" style={{ fontSize: 13 }}>
                      No per-post metrics. Reading posts and engagement needs a paid X API tier — followers/post counts show above once connected.
                    </p>
                  </Card>
                )}
                {vids.length > 0 && (
                  <Card span={12} className="reveal reveal-d2">
                    <div style={{ fontWeight: 600, marginBottom: 14 }}>Recent posts</div>
                    <div style={{ overflowX: 'auto' }}>
                      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                        <thead>
                          <tr style={{ borderBottom: '1px solid var(--border)' }}>
                            {['Post', 'Impressions', 'Likes', 'Reposts', 'Replies', 'Posted'].map((h, i) => (
                              <th key={i} style={{ textAlign: i === 0 ? 'left' : 'right', padding: '6px 10px', fontWeight: 600, color: 'var(--muted)', whiteSpace: 'nowrap' }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {vids.map((v) => (
                            <tr key={v.tweet_id} style={{ borderBottom: '1px solid var(--border-subtle, var(--border))' }}>
                              <td style={{ padding: '8px 10px', maxWidth: 360 }}>
                                <a href={v.url} target="_blank" rel="noreferrer" style={{ color: 'inherit', textDecoration: 'none', fontWeight: 500 }}>{v.text || '(no text)'}</a>
                              </td>
                              <td style={{ textAlign: 'right', padding: '8px 10px', fontWeight: 600 }}>{fmtNum(v.impression_count)}</td>
                              <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{fmtNum(v.like_count)}</td>
                              <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{fmtNum(v.retweet_count)}</td>
                              <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)' }}>{fmtNum(v.reply_count)}</td>
                              <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>{fmtDate(v.created_at)}</td>
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

      {view === 'comments' && (
        <div className="bento">
          <Card span={12} well className="reveal reveal-d1">
            <div className="row center between">
              <span className="muted" style={{ fontSize: 13 }}>Pull recent mentions and rank video requests. Reading mentions needs a paid X API tier.</span>
              <Button variant="ghost" icon="rotate" disabled={busy === 'fetch'} onClick={fetchEvaluate}>
                {busy === 'fetch' ? 'Fetching…' : 'Fetch & evaluate'}
              </Button>
            </div>
          </Card>
          {comments.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No mentions cached. Click <strong>Fetch &amp; evaluate</strong> to pull and rank recent mentions.</p></Card>}
          {comments.map((c, i) => (
            <Card key={c.comment_id || i} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
              <div className="row center between">
                <span className="row center gap-10">
                  <span style={{ fontWeight: 700 }}>{c.commenter ? `@${c.commenter}` : 'user'}</span>
                  {multi && accountName(c.channel) && <Chip tone="neutral">{accountName(c.channel)}</Chip>}
                </span>
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
                        <Button variant="ghost" icon="reply" onClick={() => setReplyFor(replyFor === c.comment_id ? '' : c.comment_id)}>Reply</Button>
                        <Button variant="danger" icon="xmark" disabled={busy === 'r' + c.comment_id} onClick={() => reject(c)}>Reject</Button>
                      </div>
                      {replyFor === c.comment_id && (
                        <ReplyComposer comment={c} onCancel={() => setReplyFor('')}
                          onSent={() => { setReplyFor(''); setStatus('Reply posted.') }} />
                      )}
                    </>
                  )}
                </div>
              )}
              {!c.is_request && (
                <div className="mt-16 stack gap-10">
                  {c.engagement_status === 'draft' ? (
                    <>
                      {c.engagement_reason && <p className="muted" style={{ fontSize: 12.5, margin: 0, fontStyle: 'italic' }}>{c.engagement_reason}</p>}
                      <Field label="Suggested reply">
                        <textarea className="input" rows={3} value={drafts[c.comment_id] ?? c.engagement_draft ?? ''}
                          onChange={(e) => setDrafts((d) => ({ ...d, [c.comment_id]: e.target.value }))} />
                      </Field>
                      <div className="row gap-10 row--wrap">
                        <Button variant="primary" icon="reply" disabled={busy === 'cs' + c.comment_id} onClick={() => sendDraft(c)}>Send reply</Button>
                        <Button variant="danger" icon="xmark" disabled={busy === 'cd' + c.comment_id} onClick={() => dismissDraft(c)}>Dismiss</Button>
                      </div>
                    </>
                  ) : (
                    <div className="row center gap-10 row--wrap">
                      {c.engagement_status === 'sent' && <Chip tone="ok"><Icon name="check" style={{ fontSize: 10 }} /> replied</Chip>}
                      {c.engagement_status === 'dismissed' && <Chip tone="neutral">dismissed</Chip>}
                      <Button variant="ghost" icon="reply" onClick={() => setReplyFor(replyFor === c.comment_id ? '' : c.comment_id)}>Reply</Button>
                      {replyFor === c.comment_id && (
                        <ReplyComposer comment={c} onCancel={() => setReplyFor('')}
                          onSent={() => { setReplyFor(''); setStatus('Reply posted.') }} />
                      )}
                    </div>
                  )}
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      {view === 'publish' && <Publish initialWorkDir={initial?.workDir} go={go} />}
    </div>
  )
}
