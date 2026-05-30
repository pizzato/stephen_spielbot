import React, { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon } from '../components.jsx'
import { api } from '../api.js'

const PROMPTS = ['Roman Empire', 'Deep sea', 'How bread is made', 'The cold war']

export default function Home({ go }) {
  const [topic, setTopic] = useState('')
  const [films, setFilms] = useState([])
  const [queueCount, setQueueCount] = useState(0)

  useEffect(() => {
    api.listJobs().then((d) => setFilms(d.finished || [])).catch(() => {})
    api.getQueue().then((d) => setQueueCount((d.queue || []).filter((q) => q.status === 'pending').length)).catch(() => {})
  }, [])

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Studio</span>
          <h1 className="display-lg reveal reveal-d1">What should we make a film about?</h1>
        </div>
      </div>

      <div className="bento">
        <Card span={8} padLg className="reveal reveal-d1">
          <span className="label-sm">New film</span>
          <div className="mt-16 stack gap-16">
            <input className="input input--xl" placeholder="The rise and fall of the Roman Empire…"
              value={topic} onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && topic.trim()) go('create', { topic }) }} />
            <div className="row row--wrap center between">
              <div className="row row--wrap gap-6">
                {PROMPTS.map((s) => (
                  <button key={s} className="btn btn--quiet" style={{ padding: '8px 14px', fontSize: 13 }}
                    onClick={() => setTopic(s)}>{s}</button>
                ))}
              </div>
              <Button variant="primary" size="lg" iconRight="arrow-right"
                disabled={!topic.trim()} onClick={() => go('create', { topic })}>Start</Button>
            </div>
          </div>
        </Card>

        <Card span={4} className="reveal reveal-d2" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div className="row center gap-16">
            <img src="/assets/StephenSpielbot.png" alt="" style={{ width: 64, height: 64, borderRadius: 16, border: '1px solid var(--line)' }} />
            <div>
              <div className="h-title">Stephen Spielbot</div>
              <div className="muted" style={{ fontSize: 13 }}>AI slop video director</div>
            </div>
          </div>
          <p className="body-1 mt-16" style={{ fontSize: 14 }}>
            Give me a topic. I'll write the script, paint every scene, record the narration, score it, and cut the whole thing together.
          </p>
        </Card>

        <Card span={7} className="reveal reveal-d2">
          <div className="card__head">
            <span className="label-sm">Recent films</span>
            <button className="btn btn--quiet" style={{ padding: '6px 12px', fontSize: 12.5 }} onClick={() => go('library')}>All films →</button>
          </div>
          <div className="stream">
            {films.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '8px 0' }}>No finished films yet.</div>}
            {films.slice(0, 5).map((r, i) => (
              <a key={i} className="stream-entry" onClick={(e) => { e.preventDefault(); go('library') }} href="#">
                <span className="stream-ico" style={{ background: 'var(--ok-soft)', color: 'var(--ok)' }}><Icon name="circle-check" /></span>
                <div className="grow"><span className="stream-title">{r.label}</span></div>
                <Icon name="chevron-right" style={{ color: 'var(--ink-4)', fontSize: 12 }} />
              </a>
            ))}
          </div>
        </Card>

        <Card span={5} className="reveal reveal-d3" link onClick={() => go('queue')}>
          <div className="card__head"><span className="label-sm">Queue</span><span className="card__arrow"><Icon name="layer-group" /></span></div>
          <div className="metric">{queueCount}</div>
          <div className="muted mt-8" style={{ fontSize: 13 }}>requests waiting to render</div>
        </Card>

        <Card span={6} className="reveal reveal-d4" link onClick={() => go('youtube')}>
          <div className="card__head"><span className="label-sm">YouTube</span><span className="card__arrow"><Icon name="youtube" brand /></span></div>
          <p className="body-1" style={{ fontSize: 13.5, marginTop: 0 }}>Pull comments, evaluate requests, generate fresh ideas, and publish finished films.</p>
          <div className="row center gap-10 mt-16"><Chip tone="ok" dot>@StephenSpielbot</Chip></div>
        </Card>

        <Card span={6} className="reveal reveal-d4" link onClick={() => go('create')}>
          <div className="card__head"><span className="label-sm">Create</span><span className="card__arrow"><Icon name="wand-magic-sparkles" /></span></div>
          <p className="body-1" style={{ fontSize: 13.5, marginTop: 0 }}>Start from a blank page — set the title, scene count, voice and visual style.</p>
          <div className="row center gap-10 mt-16"><Chip tone="accent" dot>New film</Chip></div>
        </Card>
      </div>
    </div>
  )
}
