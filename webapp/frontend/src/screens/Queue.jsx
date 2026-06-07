import React, { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner, Field } from '../components.jsx'
import { api } from '../api.js'

const STATUS_CHIP = {
  pending: ['accent', 'Queued'], creating: ['info', 'Rendering'], running: ['info', 'Rendering'],
  done: ['warn', 'Ready to publish'], upload_pending: ['warn', 'Ready to publish'],
  posted: ['ok', 'Posted'], failed: ['danger', 'Failed'], cancelled: ['neutral', 'Cancelled'],
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }

export default function Queue({ go, onEditScript }) {
  const [items, setItems] = useState([])
  const [progress, setProgress] = useState(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [editId, setEditId] = useState('')         // pending item open for inline edit
  const [draft, setDraft] = useState({ final_title: '', video_prompt: '', suggested_scene_count: 6 })

  const refresh = () => Promise.all([
    api.getQueue(),
    api.getProgress(''),
  ]).then(([q, p]) => {
    setItems(q.queue || [])
    setProgress(p || null)
    setLoaded(true)
  }).catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => { refresh() }, [])

  const run = async (key, fn, after) => {
    setBusy(key); setError(''); setStatus('')
    try { const r = await fn(); if (after) after(r); await refresh() }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const startEdit = (it) => {
    setEditId(it.id); setError(''); setStatus('')
    setDraft({
      final_title: it.final_title || it.title || '',
      video_prompt: it.video_prompt || it.comment_text || '',
      suggested_scene_count: it.suggested_scene_count || 6,
    })
  }
  const saveEdit = (id) => run('save' + id, () => api.queueUpdate(id, {
    final_title: draft.final_title,
    video_prompt: draft.video_prompt,
    suggested_scene_count: Number(draft.suggested_scene_count) || 6,
  }), () => { setEditId(''); setStatus('Queue item updated.') })

  // Open a pending item's script for editing (loads the Script tab, or the
  // Create form for items with no script yet). `overrides` carries unsaved
  // inline-edit values into the Create prefill. Navigates away on success.
  const openScript = async (it, overrides) => {
    setBusy('e' + it.id); setError('')
    try { await onEditScript(overrides ? { ...it, ...overrides } : it) }
    catch (e) { setError(e.message); setBusy('') }
  }

  const pendingItems = items.filter((i) => i.status === 'pending')
  const renderingItems = items.filter((i) => ['creating', 'running'].includes(i.status))
  const readyItems = items.filter((i) => ['done', 'upload_pending'].includes(i.status))
  const historyItems = items.filter((i) => ['posted', 'cancelled', 'failed'].includes(i.status))
  const renderActive = progress && !progress.done && progress.work_dir && progress.status === 'running'
  const hasRenderQueueItem = renderActive && renderingItems.some((i) => i.work_dir === progress.work_dir)

  const counts = {
    pending: pendingItems.length,
    creating: renderingItems.length + (renderActive && !hasRenderQueueItem ? 1 : 0),
    ready: readyItems.length,
    posted: items.filter((i) => i.status === 'posted').length,
  }

  const editPanel = (it) => (
    <div style={{ padding: '4px 22px 18px', background: 'var(--paper-2)', borderBottom: '1px solid var(--line)' }}>
      <div className="stack gap-16">
        <Field label="Title">
          <input className="input" value={draft.final_title} onChange={(e) => setDraft((d) => ({ ...d, final_title: e.target.value }))} />
        </Field>
        <Field label="Prompt / direction" hint="What the video should cover — used to draft the script.">
          <textarea className="textarea" rows={3} value={draft.video_prompt} onChange={(e) => setDraft((d) => ({ ...d, video_prompt: e.target.value }))} />
        </Field>
        <div className="row center gap-16 row--wrap">
          <Field label="Scenes">
            <input className="input" type="number" min={6} max={50} style={{ width: 110 }} value={draft.suggested_scene_count}
              onChange={(e) => setDraft((d) => ({ ...d, suggested_scene_count: e.target.value }))} />
          </Field>
          <div className="grow" />
          <div className="row gap-10 row--wrap">
            <Button variant="ghost" disabled={!!busy} onClick={() => setEditId('')}>Cancel</Button>
            <Button variant="ghost" icon="floppy-disk" disabled={!!busy || !draft.final_title.trim()} onClick={() => saveEdit(it.id)}>{busy === 'save' + it.id ? 'Saving…' : 'Save'}</Button>
            <Button variant="primary" iconRight="feather-pointed" disabled={!!busy}
              onClick={() => openScript(it, { final_title: draft.final_title, video_prompt: draft.video_prompt, suggested_scene_count: Number(draft.suggested_scene_count) || 6 })}>
              {busy === 'e' + it.id ? 'Opening…' : 'Create script →'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )

  const queueRow = (it, idx, sectionItems, { dim = false } = {}) => {
    const [tone, label] = STATUS_CHIP[it.status] || ['neutral', it.status]
    const titleText = it.final_title || it.title || '(untitled)'
    const scenes = it.suggested_scene_count
    const isPending = it.status === 'pending'
    const editing = editId === it.id
    return (
      <React.Fragment key={it.id || idx}>
        <div className="row center" style={{ gap: 14, padding: '14px 22px', borderBottom: editing || idx < sectionItems.length - 1 ? '1px solid var(--line)' : 'none', opacity: dim ? 0.62 : 1 }}>
          <div className="stack" style={{ gap: 2 }}>
            <button className="qmove" disabled={!isPending || !!busy} onClick={() => run('m' + it.id, () => api.queueMove(it.id, -1))}><Icon name="chevron-up" /></button>
            <button className="qmove" disabled={!isPending || !!busy} onClick={() => run('m' + it.id, () => api.queueMove(it.id, 1))}><Icon name="chevron-down" /></button>
          </div>
          <div className="grow">
            <div style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{titleText}</div>
            <div className="row center gap-10 mt-8" style={{ flexWrap: 'wrap' }}>
              {it.source && <Chip tone="info">{it.source}</Chip>}
              {it.interestingness != null && <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(it.interestingness).toFixed(1)}</span>}
              {scenes ? <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {tier(scenes)}</span> : null}
              {it.commenter && <span className="muted" style={{ fontSize: 12.5 }}>· {it.commenter}</span>}
            </div>
          </div>
          {isPending && it.script_ready && <Chip tone="ok" dot>Script ready</Chip>}
          <Chip tone={tone} dot>{label}</Chip>
          <div className="row gap-6 row--wrap">
            {isPending && it.script_ready && <Button variant="ghost" icon="feather-pointed" disabled={!!busy} onClick={() => openScript(it)}>{busy === 'e' + it.id ? 'Opening…' : 'Edit script'}</Button>}
            {isPending && !it.script_ready && <Button variant="ghost" icon="pencil" disabled={!!busy} onClick={() => editing ? setEditId('') : startEdit(it)}>Edit</Button>}
            {isPending && <Button variant="primary" icon="play" disabled={!!busy} onClick={() => run('s' + it.id, () => api.queueStart(it.id), () => { setStatus('Render started.'); go('progress') })}>Render now</Button>}
            {it.status === 'creating' && <Button variant="ghost" icon="stop" disabled={!!busy} onClick={() => run('d' + it.id, () => api.queueAbandon(it.id))}>Cancel</Button>}
            {['done', 'upload_pending'].includes(it.status) && <Button variant="ghost" icon="youtube" onClick={() => go('youtube')}>Publish</Button>}
            {it.status === 'posted' && it.comment_id && !it.completion_replied && <Button variant="ghost" icon="reply" disabled={!!busy} onClick={() => run('r' + it.id, () => api.queueRetryReply(it.id), () => setStatus('Reply sent.'))}>Retry reply</Button>}
            {(isPending || it.status === 'creating' || ['done', 'upload_pending', 'posted', 'cancelled'].includes(it.status)) &&
              <button className="qmove qmove--lg" disabled={!!busy} onClick={() => run('d' + it.id, () => it.status === 'creating' ? api.queueAbandon(it.id) : api.queueRemove(it.id))} title="Remove"><Icon name="trash-can" /></button>}
          </div>
        </div>
        {editing && editPanel(it)}
      </React.Fragment>
    )
  }

  const section = (title, hint, sectionItems, empty, opts = {}) => (
    <Card span={12} className="reveal reveal-d3" style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
        <span className="label-sm">{title}</span>
        {hint ? <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>{hint}</span> : null}
      </div>
      {loaded && sectionItems.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>{empty}</div>}
      {sectionItems.map((it, idx) => queueRow(it, idx, sectionItems, opts))}
    </Card>
  )

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Queue</span>
          <h1 className="display-md reveal reveal-d1">Video request queue</h1>
        </div>
        <Button variant="ghost" icon="wand-magic-sparkles" onClick={() => go('create')}>Add manually</Button>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        {/* Automation — on-demand steps (the always-on loop is driven by the Settings toggles). */}
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center between row--wrap gap-16">
            <div className="row center gap-10">
              <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="robot" /></span>
              <div><div style={{ fontWeight: 600 }}>Manual controls</div><div className="muted" style={{ fontSize: 12.5 }}>Do it now. Hands-free automation is configured in Settings → YouTube automation.</div></div>
            </div>
            <div className="row gap-10 row--wrap">
              <Button variant="primary" icon="play" disabled={!!busy} onClick={() => run('start', api.autoStart, (r) => setStatus(r.started ? `Started: ${r.started.title}` : 'Nothing to start — a render may be running, or the queue is empty.'))}>{busy === 'start' ? 'Starting…' : 'Start next render'}</Button>
              <Button variant="ghost" icon="youtube" disabled={!!busy} onClick={() => run('post', api.autoPost, (r) => setStatus(`Posted ${r.posted.length} video(s).`))}>{busy === 'post' ? 'Posting…' : 'Post finished'}</Button>
            </div>
          </div>
        </Card>

        <Card span={3} className="reveal reveal-d1"><span className="label-sm">Queued</span><div className="metric mt-8">{counts.pending}</div><div className="muted" style={{ fontSize: 13 }}>waiting to render</div></Card>
        <Card span={3} className="reveal reveal-d2"><span className="label-sm">Rendering</span><div className="metric mt-8">{counts.creating}</div><div className="muted" style={{ fontSize: 13 }}>in progress now</div></Card>
        <Card span={3} className="reveal reveal-d3"><span className="label-sm">Ready</span><div className="metric mt-8">{counts.ready}</div><div className="muted" style={{ fontSize: 13 }}>waiting to publish</div></Card>
        <Card span={3} className="reveal reveal-d3"><span className="label-sm">Posted</span><div className="metric mt-8">{counts.posted}</div><div className="muted" style={{ fontSize: 13 }}>live on YouTube</div></Card>

        {(renderActive || renderingItems.length > 0) && (
          <Card span={12} className="reveal reveal-d2" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
              <span className="label-sm">Rendering now</span>
              <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>Active work, not the waiting queue.</span>
            </div>
            {renderActive && !hasRenderQueueItem && (
              <div className="row center" style={{ gap: 14, padding: '14px 22px', borderBottom: renderingItems.length ? '1px solid var(--line)' : 'none' }}>
                <div className="grow">
                  <div style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{progress.title || 'Rendering'}</div>
                  <div className="muted mt-8" style={{ fontSize: 12.5 }}>{Math.round(progress.pct || 0)}% · {progress.msg || 'Running'}</div>
                </div>
                <Chip tone="info" dot>Rendering</Chip>
                <Button variant="ghost" icon="gauge-high" onClick={() => go('progress')}>View render</Button>
              </div>
            )}
            {renderingItems.map((it, idx) => queueRow(it, idx, renderingItems))}
          </Card>
        )}

        {section('Up next', 'Only waiting items. Comment requests rank above ideas. Reorder with the arrows.', pendingItems, 'The queue is empty. Approve a comment request (YouTube tab) or add one manually.')}
        {readyItems.length > 0 && section('Ready to publish', 'Finished videos waiting for YouTube upload.', readyItems, '', {})}
        {historyItems.length > 0 && section('History', 'Already posted or no longer active.', historyItems, '', { dim: true })}
      </div>
    </div>
  )
}
