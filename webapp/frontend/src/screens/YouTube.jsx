import React, { useState, useEffect } from 'react'
import { Card, Chip, Button, Field, Segmented, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'
import Publish from './Publish.jsx'

function Stars({ value }) {
  if (value == null) return null
  return <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(value).toFixed(1)}</span>
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }

export default function YouTube({ go, initial }) {
  const [view, setView] = useState(initial?.view || 'comments')
  const [comments, setComments] = useState([])
  const [ideas, setIdeas] = useState([])
  const [error, setError] = useState('')
  const [loadingIdeas, setLoadingIdeas] = useState(false)
  const [myIdea, setMyIdea] = useState('')

  useEffect(() => {
    api.getComments().then((d) => setComments(d.comments || [])).catch((e) => setError(e.message))
  }, [])

  const loadIdeas = async () => {
    setLoadingIdeas(true); setError('')
    try { const d = await api.getSuggestions(); setIdeas(d.suggestions || []) }
    catch (e) { setError(e.message) } finally { setLoadingIdeas(false) }
  }

  // Add a topic the user typed themselves to the idea list (same actions as AI ideas).
  const addMyIdea = () => {
    const t = myIdea.trim()
    if (!t) return
    setIdeas((prev) => [{ title: t, reason: 'Your suggestion', source: 'manual' }, ...prev])
    setMyIdea('')
  }

  useEffect(() => { if (view === 'ideas' && ideas.length === 0 && !loadingIdeas) loadIdeas() }, [view])
  // Honour a deep-link (e.g. "Publish" from a Films card) after mount.
  useEffect(() => { if (initial?.view) setView(initial.view) }, [initial])

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">YouTube</span>
          <h1 className="display-md reveal reveal-d1">Comments, ideas & publishing</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1"><Chip tone="ok" dot>@StephenSpielbot</Chip></div>
      </div>

      <Banner tone="danger">{error}</Banner>

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={setView} options={[
          { value: 'comments', label: 'Comments' }, { value: 'ideas', label: 'AI ideas' }, { value: 'publish', label: 'Publish' },
        ]} />
      </div>

      {view === 'comments' && (
        <div className="bento">
          {comments.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No comments cached. Fetch & evaluate comments from the Config tab in the classic app, then refresh here.</p></Card>}
          {comments.map((c, i) => {
            const isReq = c.is_request
            return (
              <Card key={i} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
                <div className="row center between">
                  <span style={{ fontWeight: 700 }}>{c.author || c.who || 'viewer'}</span>
                  {isReq ? <Chip tone="ok"><Icon name="check" style={{ fontSize: 10 }} /> Request</Chip> : <Chip tone="neutral">Not a request</Chip>}
                </div>
                <p className="body-1" style={{ fontSize: 14, margin: '10px 0 0' }}>{c.text || c.comment}</p>
                {isReq && (
                  <div className="mt-16">
                    {c.suggested_title || c.final_title ? <div style={{ fontWeight: 600 }}>{c.final_title || c.suggested_title}</div> : null}
                    <div className="row center gap-16 mt-8" style={{ flexWrap: 'wrap' }}>
                      <Stars value={c.interestingness} />
                      {c.confidence != null && <span className="muted" style={{ fontSize: 12.5 }}>conf {Math.round(c.confidence * 100)}%</span>}
                      {c.n_scenes ? <span className="muted" style={{ fontSize: 12.5 }}>{c.n_scenes} scenes · {tier(c.n_scenes)}</span> : null}
                      {c.status && <Chip tone={c.status === 'approved' ? 'ok' : c.status === 'rejected' ? 'danger' : 'accent'}>{c.status}</Chip>}
                    </div>
                    {c.reason && <p className="muted" style={{ fontSize: 12.5, margin: '12px 0 0', fontStyle: 'italic' }}>{c.reason}</p>}
                  </div>
                )}
              </Card>
            )
          })}
        </div>
      )}

      {view === 'ideas' && (
        <div className="bento">
          <Card span={12} well className="reveal reveal-d1">
            <div className="row center between row--wrap gap-16">
              <div className="row center gap-10">
                <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="lightbulb" /></span>
                <div><div style={{ fontWeight: 600 }}>Topic ideas</div><div className="muted" style={{ fontSize: 12.5 }}>Suggest your own, or let the AI generate them from your channel's gaps.</div></div>
              </div>
              <Button variant="ghost" icon="wand-magic-sparkles" disabled={loadingIdeas} onClick={loadIdeas}>{loadingIdeas ? 'Thinking…' : 'Generate more'}</Button>
            </div>
            <div className="row gap-10 center mt-16">
              <div className="grow">
                <input className="input" placeholder="✍️ Suggest a topic — e.g. How sourdough actually works"
                  value={myIdea} onChange={(e) => setMyIdea(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') addMyIdea() }} />
              </div>
              <Button variant="primary" icon="plus" disabled={!myIdea.trim()} onClick={addMyIdea}>Add idea</Button>
            </div>
          </Card>
          {ideas.map((idea, i) => {
            const title = idea.title || idea.final_title || idea
            const scenes = idea.n_scenes || idea.scenes
            return (
              <Card key={i} span={6} className={`reveal reveal-d${(i % 3) + 1}`}>
                <div className="row center between">
                  <span style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{title}</span>
                  {idea.source === 'manual' ? <Chip tone="accent">Your idea</Chip> : <Stars value={idea.interestingness} />}
                </div>
                {idea.reason && <p className="muted" style={{ fontSize: 13, margin: '10px 0 0', fontStyle: 'italic' }}>{idea.reason}</p>}
                {scenes ? <div className="row center between mt-16"><span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {tier(scenes)}</span><Button variant="primary" icon="wand-magic-sparkles" onClick={() => go('create', { topic: title })}>Create</Button></div>
                  : <div className="row mt-16"><Button variant="primary" icon="wand-magic-sparkles" onClick={() => go('create', { topic: title })}>Create</Button></div>}
              </Card>
            )
          })}
        </div>
      )}

      {view === 'publish' && <Publish initialWorkDir={initial?.workDir} />}
    </div>
  )
}
