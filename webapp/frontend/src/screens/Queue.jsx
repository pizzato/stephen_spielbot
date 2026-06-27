import { useState, useEffect, Fragment } from 'react'
import { Card, Chip, Button, Icon, Banner, Field, ResolutionPicker } from '../components.jsx'
import { api } from '../api.js'

const STATUS_CHIP = {
  pending: ['accent', 'Queued'], creating: ['info', 'Rendering'], running: ['info', 'Rendering'],
  done: ['warn', 'Ready to publish'], upload_pending: ['warn', 'Ready to publish'],
  posted: ['ok', 'Posted'], failed: ['danger', 'Failed'], cancelled: ['neutral', 'Cancelled'],
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }
function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(n)
}

// Sorts for the waiting queue. The picked sort is saved to config
// (queue_sort_order) and is the real consumption order: automation and
// "Start next render" start the item at the top of this view. "Queue order"
// is the manual order (reorder with the arrows).
const SORT_OPTIONS = [
  { value: 'queue', label: 'Queue order' },
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' },
  { value: 'interest', label: 'Interestingness' },
  { value: 'views', label: 'Predicted views' },
  { value: 'fastest', label: 'Fastest render' },
]
const validSort = (v) => (SORT_OPTIONS.some((o) => o.value === v) ? v : 'queue')

export default function Queue({ go, onEditScript, meta = {} }) {
  const [items, setItems] = useState([])
  const [progress, setProgress] = useState(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [editId, setEditId] = useState('')         // pending item open for inline edit
  const [draft, setDraft] = useState({ final_title: '', video_prompt: '', suggested_scene_count: 6, gen_resolution: '', gen_style_name: '' })
  const [sortBy, setSortBy] = useState(() => validSort(meta.config?.queue_sort_order))
  // id -> predicted 3-day views (null = model unavailable, undefined = not fetched yet)
  const [views, setViews] = useState({})
  const styleList = meta.config?.styles || []

  // Resolution the item would render at — its own, else its style's, else the default.
  const effectiveResolution = (it) => {
    const st = styleList.find((s) => s.name === (it.gen_style_name || meta.config?.default_style))
    return it.gen_resolution || st?.resolution || meta.config?.resolution || meta.default_resolution || ''
  }

  const refresh = () => Promise.all([
    api.getQueue(),
    api.getProgress(''),
  ]).then(([q, p]) => {
    setItems(q.queue || [])
    setProgress(p || null)
    setLoaded(true)
  }).catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => { refresh() }, [])

  // The saved sort is authoritative; `meta` can be stale (App fetches config
  // once, before any change made on this page), so re-read it on mount.
  useEffect(() => {
    api.getConfig().then((m) => {
      const v = m?.config?.queue_sort_order
      if (v) setSortBy(validSort(v))
    }).catch(() => {})
  }, [])

  const changeSort = (v) => {
    setSortBy(v)
    api.saveConfig({ queue_sort_order: v }).catch((e) => setError(e.message))
  }

  // Predicted reach per waiting item (same engagement model as the Ideas tab).
  // Renders nothing until a model is built; null marks "asked, unavailable".
  useEffect(() => {
    const missing = items.filter((i) => i.status === 'pending' && i.id && views[i.id] === undefined)
    if (!missing.length) return
    let live = true
    Promise.all(missing.map((it) =>
      api.engagementPredict({
        title: it.final_title || it.title || '',
        description: it.video_prompt || it.comment_text || '',
        is_short: effectiveResolution(it).startsWith('Portrait'),
        style_name: it.gen_style_name || '',
      }).then((r) => [it.id, r?.available ? r.predicted_views : null]).catch(() => [it.id, null])
    )).then((pairs) => { if (live) setViews((v) => ({ ...v, ...Object.fromEntries(pairs) })) })
    return () => { live = false }
  }, [items])

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
      gen_resolution: it.gen_resolution || meta.config?.resolution || meta.default_resolution || '',
      gen_style_name: it.gen_style_name || meta.config?.default_style || '',
    })
  }
  const saveEdit = (id) => run('save' + id, () => api.queueUpdate(id, {
    final_title: draft.final_title,
    video_prompt: draft.video_prompt,
    suggested_scene_count: Number(draft.suggested_scene_count) || 6,
    gen_resolution: draft.gen_resolution || '',
    gen_style_name: draft.gen_style_name || '',
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
  const SORT_FNS = {
    newest: (a, b) => (b.created_at || 0) - (a.created_at || 0),
    oldest: (a, b) => (a.created_at || 0) - (b.created_at || 0),
    interest: (a, b) => (b.interestingness ?? -1) - (a.interestingness ?? -1),
    views: (a, b) => (views[b.id] ?? -1) - (views[a.id] ?? -1),
    fastest: (a, b) => (a.est_seconds ?? Number.MAX_SAFE_INTEGER) - (b.est_seconds ?? Number.MAX_SAFE_INTEGER),
  }
  const sortedPending = SORT_FNS[sortBy] ? [...pendingItems].sort(SORT_FNS[sortBy]) : pendingItems
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
        {/* Top-aligned so fields with hints don't push the others off their
            labels (the old `center` row left Scenes/Style misaligned). */}
        <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-start' }}>
          <Field label="Scenes">
            <input className="input" type="number" min={6} max={50} style={{ width: 110 }} value={draft.suggested_scene_count}
              onChange={(e) => setDraft((d) => ({ ...d, suggested_scene_count: e.target.value }))} />
          </Field>
          {styleList.length > 0 && (
            <Field label="Style" hint="Script, render and audio settings.">
              <select className="select" value={draft.gen_style_name} onChange={(e) => setDraft((d) => ({ ...d, gen_style_name: e.target.value }))}>
                {styleList.map((s) => <option key={s.name} value={s.name}>{s.name}{meta.config?.default_style === s.name ? ' (default)' : ''}</option>)}
                <option value="(none)">No style — experiment</option>
              </select>
            </Field>
          )}
          <Field label="Resolution" hint="Orientation, then quality (higher = slower).">
            <ResolutionPicker value={draft.gen_resolution} onChange={(r) => setDraft((d) => ({ ...d, gen_resolution: r }))} meta={meta} />
          </Field>
        </div>
        <div className="row gap-10 row--wrap" style={{ justifyContent: 'flex-end' }}>
          <Button variant="ghost" disabled={!!busy} onClick={() => setEditId('')}>Cancel</Button>
          <Button variant="ghost" icon="floppy-disk" disabled={!!busy || !draft.final_title.trim()} onClick={() => saveEdit(it.id)}>{busy === 'save' + it.id ? 'Saving…' : 'Save'}</Button>
          <Button variant="primary" iconRight="feather-pointed" disabled={!!busy}
            onClick={() => openScript(it, { final_title: draft.final_title, video_prompt: draft.video_prompt, suggested_scene_count: Number(draft.suggested_scene_count) || 6, gen_resolution: draft.gen_resolution || '', gen_style_name: draft.gen_style_name || '' })}>
            {busy === 'e' + it.id ? 'Opening…' : 'Create script →'}
          </Button>
        </div>
      </div>
    </div>
  )

  const queueRow = (it, idx, sectionItems, { dim = false, noMove = false } = {}) => {
    const [tone, label] = STATUS_CHIP[it.status] || ['neutral', it.status]
    const titleText = it.final_title || it.title || '(untitled)'
    const scenes = it.suggested_scene_count
    const isPending = it.status === 'pending'
    const editing = editId === it.id
    // The style this item renders with: its own, else (while still pending)
    // the default style — which is what the render will resolve.
    const renderStyle = it.gen_style_name || (isPending ? meta.config?.default_style : '')
    const styleLabel = renderStyle === '(none)' ? 'No style' : renderStyle
    // The approval gate only bites when auto-start is on (it decides which
    // ready scripts the loop renders). We surface approval state regardless so
    // you can see — and pre-set — which scripts are approved; with auto-start
    // off it's just a flag, so "Render now" stays the primary action.
    const autoStartOn = !!meta.config?.youtube_auto_start_job
    const needsApproval = isPending && it.script_ready && !it.approved
    return (
      <Fragment key={it.id || idx}>
        <div className="row center qrow" style={{ gap: 14, padding: '14px 22px', borderBottom: editing || idx < sectionItems.length - 1 ? '1px solid var(--line)' : 'none', opacity: dim ? 0.62 : 1 }}>
          <div className="stack" style={{ gap: 2 }} title={noMove ? 'Switch sort to Queue order to reorder' : undefined}>
            <button className="qmove" disabled={!isPending || !!busy || noMove} onClick={() => run('m' + it.id, () => api.queueMove(it.id, -1))}><Icon name="chevron-up" /></button>
            <button className="qmove" disabled={!isPending || !!busy || noMove} onClick={() => run('m' + it.id, () => api.queueMove(it.id, 1))}><Icon name="chevron-down" /></button>
          </div>
          <div className="grow">
            <div style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{titleText}</div>
            <div className="row center gap-10 mt-8" style={{ flexWrap: 'wrap' }}>
              {it.source && <Chip tone="info">{it.source}</Chip>}
              {styleLabel && <Chip tone="accent"><Icon name="palette" style={{ fontSize: 10 }} /> {styleLabel}</Chip>}
              {it.interestingness != null && <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(it.interestingness).toFixed(1)}</span>}
              {views[it.id] != null && <span title="Predicted 3-day views"><Chip tone="accent"><Icon name="chart-line" style={{ fontSize: 10 }} /> ~{fmtNum(views[it.id])}</Chip></span>}
              {scenes ? <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {tier(scenes)}</span> : null}
              {it.est_text && <span className="muted" style={{ fontSize: 12.5 }} title={it.est_confidence === 'rough' ? 'Estimated render time (rough — little timing data for this setup yet)' : 'Estimated render time, from your past renders'}><Icon name="clock" style={{ fontSize: 11 }} /> {it.est_text}</span>}
              {it.commenter && <span className="muted" style={{ fontSize: 12.5 }}>· {it.commenter}</span>}
            </div>
          </div>
          {isPending && it.script_ready && <Chip tone={it.approved ? 'ok' : 'warn'} dot>{it.approved ? 'Approved' : 'Needs review'}</Chip>}
          <Chip tone={tone} dot>{label}</Chip>
          <div className="row gap-6 row--wrap qrow__actions">
            {isPending && it.script_ready && <Button variant="ghost" icon="feather-pointed" disabled={!!busy} onClick={() => openScript(it)}>{busy === 'e' + it.id ? 'Opening…' : 'Edit script'}</Button>}
            {isPending && !it.script_ready && <Button variant="ghost" icon="pencil" disabled={!!busy} onClick={() => editing ? setEditId('') : startEdit(it)}>Edit</Button>}
            {needsApproval && <Button variant={autoStartOn ? 'primary' : 'ghost'} icon="check" disabled={!!busy} onClick={() => run('a' + it.id, () => api.queueApprove(it.id), () => setStatus(autoStartOn ? 'Approved — it will render shortly.' : 'Approved.'))}>{busy === 'a' + it.id ? 'Approving…' : 'Approve'}</Button>}
            {isPending && it.script_ready && it.approved && <Button variant="ghost" icon="rotate-left" disabled={!!busy} onClick={() => run('a' + it.id, () => api.queueApprove(it.id, false), () => setStatus('Moved back to review.'))}>{busy === 'a' + it.id ? '…' : 'Unapprove'}</Button>}
            {isPending && <Button variant={needsApproval && autoStartOn ? 'ghost' : 'primary'} icon="play" disabled={!!busy} onClick={() => run('s' + it.id, () => api.queueStart(it.id), () => { setStatus('Render started.'); go('progress') })}>Render now</Button>}
            {it.status === 'creating' && <Button variant="ghost" icon="stop" disabled={!!busy} onClick={() => run('d' + it.id, () => api.queueAbandon(it.id))}>Cancel</Button>}
            {['done', 'upload_pending'].includes(it.status) && <Button variant="ghost" icon="upload" onClick={() => go('publish', { publishWorkDir: it.work_dir })}>Publish</Button>}
            {it.status === 'posted' && it.comment_id && !it.completion_replied && <Button variant="ghost" icon="reply" disabled={!!busy} onClick={() => run('r' + it.id, () => api.queueRetryReply(it.id), () => setStatus('Reply sent.'))}>Retry reply</Button>}
            {(isPending || it.status === 'creating' || ['done', 'upload_pending', 'posted', 'cancelled', 'failed'].includes(it.status)) &&
              <button className="qmove qmove--lg" disabled={!!busy} onClick={() => run('d' + it.id, () => it.status === 'creating' ? api.queueAbandon(it.id) : api.queueRemove(it.id))} title="Remove"><Icon name="trash-can" /></button>}
          </div>
        </div>
        {editing && editPanel(it)}
      </Fragment>
    )
  }

  const section = (title, hint, sectionItems, empty, opts = {}) => {
    const { extra, ...rowOpts } = opts
    return (
      <Card span={12} className="reveal reveal-d3" style={{ padding: 0, overflow: 'hidden' }}>
        <div className="row center between gap-10 row--wrap qrow-head" style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
          <div>
            <span className="label-sm">{title}</span>
            {hint ? <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>{hint}</span> : null}
          </div>
          {extra || null}
        </div>
        {loaded && sectionItems.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>{empty}</div>}
        {sectionItems.map((it, idx) => queueRow(it, idx, sectionItems, rowOpts))}
      </Card>
    )
  }

  const sortControl = (
    <label className="row center" style={{ gap: 8 }}>
      <span className="muted" style={{ fontSize: 12.5 }}><Icon name="arrow-down-wide-short" /> Sort</span>
      <select className="select" style={{ width: 'auto', padding: '6px 10px', fontSize: 13 }}
        value={sortBy} onChange={(e) => changeSort(e.target.value)}>
        {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
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
              <Button variant="primary" icon="play" disabled={!!busy} onClick={() => run('start', api.autoStart, (r) => setStatus(r.started ? `Started: ${r.started.title}` : 'Nothing to start — a render may be running, or no approved queued item is ready to start.'))}>{busy === 'start' ? 'Starting…' : 'Start next render'}</Button>
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
            <div className="qrow-head" style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
              <span className="label-sm">Rendering now</span>
              <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>Active work, not the waiting queue.</span>
            </div>
            {renderActive && !hasRenderQueueItem && (
              <div className="row center qrow" style={{ gap: 14, padding: '14px 22px', borderBottom: renderingItems.length ? '1px solid var(--line)' : 'none' }}>
                <div className="grow">
                  <div style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{progress.title || 'Rendering'}</div>
                  <div className="muted mt-8" style={{ fontSize: 12.5 }}>{Math.round(progress.pct || 0)}% · {progress.msg || 'Running'}{progress.eta?.eta_text ? ` · ${progress.eta.eta_text} left` : ''}</div>
                </div>
                <Chip tone="info" dot>Rendering</Chip>
                <Button variant="ghost" icon="gauge-high" onClick={() => go('progress')}>View render</Button>
              </div>
            )}
            {renderingItems.map((it, idx) => queueRow(it, idx, renderingItems))}
          </Card>
        )}

        {section('Up next',
          (sortBy === 'queue'
            ? 'Only waiting items. Comment requests rank above ideas. Reorder with the arrows.'
            : 'The next render starts from the top of this order. Switch to Queue order to reorder by hand.')
          + (meta.config?.youtube_auto_start_job ? ' Approve an item to release it to the auto-render loop.' : ''),
          sortedPending, 'The queue is empty. Approve a comment request (YouTube tab) or add one manually.',
          { extra: sortControl, noMove: sortBy !== 'queue' })}
        {readyItems.length > 0 && section('Ready to publish', 'Finished videos waiting for YouTube upload.', readyItems, '', {})}
        {historyItems.length > 0 && section('History', 'Already posted or no longer active.', historyItems, '', { dim: true })}
      </div>
    </div>
  )
}
