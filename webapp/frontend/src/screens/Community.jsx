import { useState, useEffect } from 'react'
import { Card, Chip, Button, Field, Segmented, Icon, Banner, RegenLabel, FilterSelect } from '../components.jsx'
import { api } from '../api.js'
import { useHashParams } from '../nav.js'

// Unified "Community" screen (issue #107): one comment-management UI for both
// platforms, chosen by the YouTube/X toggle. The two platforms differ only in
// which API methods are called and a couple of labels — captured in ADAPTERS —
// so the comment-card UI itself is shared.
const ADAPTERS = {
  youtube: {
    label: 'YouTube', listApi: () => api.ytChannels().then((r) => r.channels || []),
    get: api.getComments, fetch: api.fetchComments,
    approve: api.approveComment, reject: api.rejectComment, reply: api.replyComment,
    draft: api.draftCommentReply, sendDraft: api.sendCommunityReply, dismissDraft: api.dismissCommunityReply,
    prefix: '', unit: 'comments', emptyHint: 'channel comments',
  },
  x: {
    label: 'X', listApi: () => api.xAccounts().then((r) => r.accounts || []),
    get: api.xComments, fetch: api.xFetchComments,
    approve: api.xApproveComment, reject: api.xRejectComment, reply: api.xReplyComment,
    draft: null, sendDraft: api.xSendCommunityReply, dismissDraft: api.xDismissCommunityReply,
    prefix: '@', unit: 'mentions', emptyHint: 'mentions',
  },
}

function Stars({ value }) {
  if (value == null) return null
  return <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(value).toFixed(1)}</span>
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }

// Inline manual-reply composer. The "Draft with AI" button only shows for
// platforms whose adapter has a draft endpoint (YouTube does, X doesn't yet).
function ReplyComposer({ comment, adapter, onSent, onCancel }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const draft = async (instruction = '') => {
    if (!adapter.draft) return
    setBusy('draft'); setError('')
    try { const r = await adapter.draft(comment.comment_id, instruction); setText(r.reply || '') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const send = async () => {
    const t = text.trim()
    if (!t) return
    setBusy('send'); setError('')
    try { await adapter.reply(comment.comment_id, t); onSent() }
    catch (e) { setError(e.message); setBusy('') }
  }
  return (
    <div className="stack gap-10 mt-10">
      <Field label={adapter.draft
        ? <RegenLabel busy={busy === 'draft'} onRegen={(instr) => draft(instr)} label="Draft with AI" busyLabel="Drafting…"
            chips={['Shorter', 'Warmer', 'Funnier', 'More formal']}>Your reply</RegenLabel>
        : 'Your reply'}>
        <textarea className="input" rows={3} value={text} placeholder={`Reply to ${adapter.prefix}${comment.commenter || 'viewer'}…`}
          onChange={(e) => setText(e.target.value)} />
      </Field>
      {error && <p style={{ fontSize: 12, color: 'var(--danger)', margin: 0 }}>{error}</p>}
      <div className="row gap-10 row--wrap">
        <Button variant="primary" icon="reply" disabled={busy === 'send' || !text.trim()} onClick={send}>
          {busy === 'send' ? 'Sending…' : 'Send reply'}</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  )
}

export default function Community() {
  // Platform + filters live in the URL (#/community?platform=x&channel=…&status=…)
  // so they accumulate and back-navigation returns to the same filtered view.
  const [filters, setFilters] = useHashParams({ platform: 'youtube', channel: '', status: 'all' })
  const platform = ADAPTERS[filters.platform] ? filters.platform : 'youtube'
  const { channel: channelFilter, status: statusFilter } = filters
  // Switching platform drops the channel filter — the ids belong to the old one.
  const setPlatform = (v) => setFilters({ platform: v, channel: '' })
  const [comments, setComments] = useState([])
  const [targets, setTargets] = useState([])   // channels (YT) / accounts (X)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [titles, setTitles] = useState({})
  const [drafts, setDrafts] = useState({})
  const [replyFor, setReplyFor] = useState('')
  const adapter = ADAPTERS[platform]

  const refreshComments = () => adapter.get().then((d) => setComments(d.comments || [])).catch((e) => setError(e.message))

  useEffect(() => {
    setComments([]); setStatus(''); setError(''); setReplyFor('')
    refreshComments()
    adapter.listApi().then(setTargets).catch(() => setTargets([]))
  }, [platform])

  const targetName = (key) => {
    const t = targets.find((x) => x.id === key)
    return t ? (adapter.prefix + (t.name || t.id)) : ''
  }
  const multi = targets.length > 1

  const fetchEvaluate = async () => {
    setBusy('fetch'); setError(''); setStatus('')
    try {
      const r = await adapter.fetch()
      setComments(r.comments || [])
      setStatus(`Fetched ${r.new} new · ${r.auto_approved} auto-approved · ${r.thanked} thanked · ${r.community_drafted ?? 0} drafted · ${r.community_sent ?? 0} sent`)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const approve = async (c) => {
    setBusy('a' + c.comment_id); setError('')
    try {
      const r = await adapter.approve(c.comment_id, titles[c.comment_id] ?? c.suggested_title)
      setStatus(`Approved → queued: ${r.final_title}`)
      await refreshComments()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const reject = async (c) => {
    setBusy('r' + c.comment_id); setError('')
    try { await adapter.reject(c.comment_id); await refreshComments(); setStatus('Rejected.') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const regenDraft = async (c, instruction = '') => {
    if (!adapter.draft) return
    setBusy('cr' + c.comment_id); setError('')
    try { const r = await adapter.draft(c.comment_id, instruction); setDrafts((d) => ({ ...d, [c.comment_id]: r.reply || '' })) }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const sendDraft = async (c) => {
    const text = (drafts[c.comment_id] ?? c.engagement_draft ?? '').trim()
    if (!text) return
    setBusy('cs' + c.comment_id); setError('')
    try { await adapter.sendDraft(c.comment_id, text); setStatus('Reply sent.'); await refreshComments() }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  const dismissDraft = async (c) => {
    setBusy('cd' + c.comment_id); setError('')
    try { await adapter.dismissDraft(c.comment_id); await refreshComments(); setStatus('Draft dismissed.') }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const pending = (c) => c.is_request && !['approved', 'rejected'].includes(c.status)

  // Status buckets overlap (an approved request is also a request), so this is
  // a match, not a partition. "Needs action" = a request awaiting a decision or
  // a drafted reply awaiting send.
  const inBucket = (c, bucket) => {
    switch (bucket) {
      case 'requests': return !!c.is_request
      case 'action': return pending(c) || (!c.is_request && c.engagement_status === 'draft')
      case 'approved': return c.status === 'approved'
      case 'rejected': return c.status === 'rejected'
      case 'replied': return c.engagement_status === 'sent'
      default: return true
    }
  }
  const channelMatched = comments.filter((c) => !channelFilter || c.channel === channelFilter)
  const shown = channelMatched.filter((c) => inBucket(c, statusFilter))
  const statusOpts = [
    { value: 'all', label: 'All', n: channelMatched.length },
    { value: 'action', label: 'Needs action', n: 0 },
    { value: 'requests', label: 'Requests', n: 0 },
    { value: 'approved', label: 'Approved', n: 0 },
    { value: 'rejected', label: 'Rejected', n: 0 },
    { value: 'replied', label: 'Replied', n: 0 },
  ].map((o) => (o.value === 'all' ? o : { ...o, n: channelMatched.filter((c) => inBucket(c, o.value)).length }))
    .filter((o) => o.value === 'all' || o.n > 0 || o.value === statusFilter)
    .map((o) => ({ value: o.value, label: `${o.label} (${o.n})` }))
  const channelOpts = targets.map((t) => ({ value: t.id, label: adapter.prefix + (t.name || t.id) }))
  if (channelFilter && !channelOpts.some((o) => o.value === channelFilter))
    channelOpts.push({ value: channelFilter, label: channelFilter })

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Community</span>
          <h1 className="display-md reveal reveal-d1">Comments &amp; mentions</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          {multi && targetName((targets[0] || {}).id) !== '' && (
            <Chip tone="neutral" dot>{targets.length} {adapter.label} {targets.length === 1 ? 'account' : 'accounts'}</Chip>
          )}
        </div>
      </div>

      <div className="reveal reveal-d1 row center gap-10 row--wrap" style={{ marginBottom: 16 }}>
        <Segmented value={platform} onChange={setPlatform} options={[
          { value: 'youtube', label: 'YouTube' },
          { value: 'x', label: 'X' },
        ]} />
        {comments.length > 0 && <Segmented options={statusOpts} value={statusFilter} onChange={(v) => setFilters({ status: v })} />}
        {(channelOpts.length > 1 || channelFilter) && (
          <FilterSelect value={channelFilter} onChange={(v) => setFilters({ channel: v })}
            options={channelOpts} allLabel={platform === 'x' ? 'All accounts' : 'All channels'} />
        )}
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center between">
            <span className="muted" style={{ fontSize: 13 }}>
              Pull the latest {adapter.emptyHint} and rank video requests.{platform === 'x' ? ' Reading mentions needs a paid X API tier.' : ''}
            </span>
            <Button variant="ghost" icon="rotate" disabled={busy === 'fetch'} onClick={fetchEvaluate}>
              {busy === 'fetch' ? 'Fetching…' : 'Fetch & evaluate'}
            </Button>
          </div>
        </Card>
        {comments.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No {adapter.unit} cached. Click <strong>Fetch &amp; evaluate</strong> to pull and rank the latest {adapter.emptyHint}.</p></Card>}
        {comments.length > 0 && shown.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No {adapter.unit} match this filter.</p></Card>}
        {shown.map((c, i) => (
          <Card key={c.comment_id || i} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
            <div className="row center between">
              <span className="row center gap-10">
                <span style={{ fontWeight: 700 }}>{adapter.prefix}{c.commenter || 'viewer'}</span>
                {multi && targetName(c.channel) && <Chip tone="neutral">{targetName(c.channel)}</Chip>}
              </span>
              {c.is_request ? <Chip tone="ok"><Icon name="check" style={{ fontSize: 10 }} /> Request</Chip> : <Chip tone="neutral">Not a request</Chip>}
            </div>
            <p className="body-1" style={{ fontSize: 14, margin: '10px 0 0' }}>{c.text}</p>

            {(c.replies?.length > 0) && (
              <div className="stack gap-6" style={{ marginTop: 10, paddingLeft: 12, borderLeft: '2px solid var(--line)' }}>
                {c.replies.map((r, ri) => (
                  <p key={r.reply_id || ri} style={{ fontSize: 13, margin: 0 }}>
                    <span style={{ fontWeight: 600 }}>{r.is_owner ? (targetName(c.channel) || 'You') : (adapter.prefix + (r.commenter || 'viewer'))}</span>
                    <span className="muted">{' '}{r.text}</span>
                  </p>
                ))}
              </div>
            )}

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
                      <ReplyComposer comment={c} adapter={adapter} onCancel={() => setReplyFor('')}
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
                    <Field label={adapter.draft
                      ? <RegenLabel busy={busy === 'cr' + c.comment_id} onRegen={(instr) => regenDraft(c, instr)}
                          chips={['Shorter', 'Warmer', 'Funnier', 'More formal']}>Suggested reply</RegenLabel>
                      : 'Suggested reply'}>
                      <textarea className="input" rows={3} value={drafts[c.comment_id] ?? c.engagement_draft ?? ''}
                        onChange={(e) => setDrafts((d) => ({ ...d, [c.comment_id]: e.target.value }))} />
                    </Field>
                    <div className="row gap-10 row--wrap">
                      <Button variant="primary" icon="reply" disabled={busy === 'cs' + c.comment_id} onClick={() => sendDraft(c)}>Send reply</Button>
                      <Button variant="danger" icon="xmark" disabled={busy === 'cd' + c.comment_id} onClick={() => dismissDraft(c)}>Dismiss</Button>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="row center gap-10 row--wrap">
                      {c.engagement_status === 'sent' && <Chip tone="ok"><Icon name="check" style={{ fontSize: 10 }} /> replied</Chip>}
                      {c.engagement_status === 'dismissed' && <Chip tone="neutral">dismissed</Chip>}
                      <Button variant="ghost" icon="reply" onClick={() => setReplyFor(replyFor === c.comment_id ? '' : c.comment_id)}>Reply</Button>
                    </div>
                    {replyFor === c.comment_id && (
                      <ReplyComposer comment={c} adapter={adapter} onCancel={() => setReplyFor('')}
                        onSent={() => { setReplyFor(''); setStatus('Reply posted.') }} />
                    )}
                  </>
                )}
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  )
}
