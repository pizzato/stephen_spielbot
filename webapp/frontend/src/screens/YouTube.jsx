import React, { useState, useEffect } from 'react'
import { Card, Chip, Button, Field, Segmented, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'
import Publish from './Publish.jsx'

function Stars({ value }) {
  if (value == null) return null
  return <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(value).toFixed(1)}</span>
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }

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

export default function YouTube({ go, initial }) {
  const [view, setView] = useState(initial?.view || 'comments')
  const [comments, setComments] = useState([])
  const [ideas, setIdeas] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [loadingIdeas, setLoadingIdeas] = useState(false)
  const [guidance, setGuidance] = useState('')
  const [busy, setBusy] = useState('')          // action key currently running
  const [titles, setTitles] = useState({})      // per-comment edited title
  const [badges, setBadges] = useState({})      // {youtube_attention, youtube_publishable}

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
  const queueIdea = async (idea, title, scenes) => {
    setError('')
    try {
      await api.queueAdd(title, scenes, idea.reason || '')
      await closeIdea(idea, 'used')
      setStatus('Added to queue.')
      go('queue')
    } catch (e) {
      setError(e.message)
    }
  }
  const createIdea = async (idea, title, scenes) => {
    await closeIdea(idea, 'used')
    go('create', { title, description: idea.reason || '', scenes })
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">YouTube</span>
          <h1 className="display-md reveal reveal-d1">Comments, ideas &amp; publishing</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          <Chip tone="ok" dot>@StephenSpielbot</Chip>
          <Button variant="ghost" icon="rotate" disabled={busy === 'fetch'} onClick={fetchEvaluate}>
            {busy === 'fetch' ? 'Fetching…' : 'Fetch & evaluate'}</Button>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={setView} options={[
          { value: 'comments', label: badges.youtube_attention ? <span>Comments <span style={SEG_BADGE}>{badges.youtube_attention}</span></span> : 'Comments' },
          { value: 'ideas', label: 'AI ideas' },
          { value: 'publish', label: badges.youtube_publishable ? <span>Publish <span style={SEG_BADGE}>{badges.youtube_publishable}</span></span> : 'Publish' },
        ]} />
      </div>

      {view === 'comments' && (
        <div className="bento">
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
            <div className="row gap-10 center mt-16">
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
            const scenes = idea.suggested_scene_count || idea.n_scenes || idea.scenes || 12
            const key = ideaKey(idea) || `${title}-${i}`
            return (
              <Card key={key} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
                <div className="row center between">
                  <span style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</span>
                  <Stars value={idea.interestingness} />
                </div>
                {idea.reason && <p className="muted" style={{ fontSize: 13, margin: '10px 0 0', fontStyle: 'italic' }}>{idea.reason}</p>}
                <div className="row center between mt-16 row--wrap gap-10">
                  <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {tier(scenes)}</span>
                  <div className="row gap-10">
                    <Button variant="ghost" icon="xmark" disabled={busy === 'idea-' + key} onClick={() => closeIdea(idea)}>Close</Button>
                    <Button variant="ghost" icon="layer-group" disabled={busy === 'idea-' + key} onClick={() => queueIdea(idea, title, scenes)}>Queue</Button>
                    <Button variant="primary" icon="wand-magic-sparkles" disabled={busy === 'idea-' + key} onClick={() => createIdea(idea, title, scenes)}>Create</Button>
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {view === 'publish' && <Publish initialWorkDir={initial?.workDir} />}
    </div>
  )
}
