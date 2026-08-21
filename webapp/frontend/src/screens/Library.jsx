import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner, Segmented, FilterSelect } from '../components.jsx'
import { api } from '../api.js'
import { useHashParams } from '../nav.js'

// Publish-state bucket for a finished film, mirroring the card chip priority
// (published → needs approval → approved), so every film lands in exactly one
// bucket and the filter counts add up to the total.
const statusOf = (f) =>
  f.published ? 'published'
  : f.awaiting_approval ? 'approval'
  : f.approved ? 'approved'
  : 'unpublished'

// "Landscape FHD (1920×1080)" → "1920×1080": rendering one script at a second
// resolution leaves two films sharing a label, so the card wears its size.
const resBadge = (r) => {
  const m = /\((\d+)[×x](\d+)\)/.exec(r || '')
  return m ? `${m[1]}×${m[2]}` : (r || '')
}

export default function Library({ go, onOpenProgress, onOpenEdit, onNewVersion }) {
  const [jobs, setJobs] = useState({ finished: [], scripts: [], resumable: [] })
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [confirmDel, setConfirmDel] = useState('')   // work_dir pending delete confirm
  const [busyDel, setBusyDel] = useState('')
  const [busyNew, setBusyNew] = useState('')         // work_dir pending "new version" copy
  const [busyApprove, setBusyApprove] = useState('') // work_dir pending publish approval
  // Filters live in the URL (#/films?status=…&channel=…&style=…) so they
  // accumulate and back-navigation returns to the same filtered view.
  const [{ status: filter, channel, style }, setFilters] = useHashParams({ status: 'all', channel: '', style: '' })
  const setFilter = (v) => setFilters({ status: v })

  const load = () => api.listJobs().then((d) => { setJobs(d); setLoaded(true) }).catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => { load() }, [])

  // Entering a finished film clears its "New" badge, then opens the Edit film
  // screen (Film tab — final cut + details). Fire-and-forget on the badge.
  const openCard = (wd) => { api.markJobSeen(wd).catch(() => {}); onOpenEdit(wd) }

  // Copy a finished film's script into a fresh work dir and open the editor on it.
  // On success onNewVersion navigates away (this screen unmounts), so busy is only
  // cleared on error.
  const newVersion = async (wd) => {
    setBusyNew(wd); setError('')
    try { await onNewVersion(wd) }
    catch (e) { setError(e.message); setBusyNew('') }
  }

  // Approve a finished film for publishing (publish_require_approval gate). Once
  // approved it releases on the normal schedule/cadence — no manual posting.
  const approve = async (wd) => {
    setBusyApprove(wd); setError('')
    try { await api.publishApprove(wd); await load() }
    catch (e) { setError(e.message) } finally { setBusyApprove('') }
  }

  // Delete a partial render / unfinished job and its files (cancels it first if running).
  const del = async (wd) => {
    setBusyDel(wd); setError('')
    try { await api.deleteJob(wd); setConfirmDel(''); await load() }
    catch (e) { setError(e.message) } finally { setBusyDel('') }
  }

  // Channel/style options come from the films themselves, so the dropdowns only
  // offer values that actually select something. Filters accumulate: the status
  // buckets (and their counts) are computed on the channel/style-narrowed set.
  // A stale URL value (channel/style no longer present) stays listed so the
  // empty result is explicable — and clearable — rather than silently ignored.
  const distinct = (key, cur) => {
    const vals = [...new Set(jobs.finished.map((f) => f[key]).filter(Boolean))].sort()
    if (cur && !vals.includes(cur)) vals.push(cur)
    return vals
  }
  const channelOpts = distinct('channel', channel)
  const styleOpts = distinct('style_name', style)
  const base = jobs.finished.filter((f) =>
    (!channel || f.channel === channel) && (!style || f.style_name === style))

  // Filter options carry counts; a bucket only appears once it has films (or is
  // the current selection, so it doesn't vanish under the user when the last
  // match moves on). With the approval gate off, the approval buckets never show.
  const counts = base.reduce((m, f) => { const s = statusOf(f); m[s] = (m[s] || 0) + 1; return m }, {})
  const filterOpts = [
    { value: 'all', label: 'All', n: base.length },
    { value: 'approval', label: 'Needs approval', n: counts.approval || 0 },
    { value: 'approved', label: 'Approved', n: counts.approved || 0 },
    { value: 'published', label: 'Published', n: counts.published || 0 },
    { value: 'unpublished', label: 'Not published', n: counts.unpublished || 0 },
  ].filter((o) => o.value === 'all' || o.n > 0 || o.value === filter)
    .map((o) => ({ value: o.value, label: `${o.label} (${o.n})` }))
  const shown = filter === 'all' ? base : base.filter((f) => statusOf(f) === filter)
  const unfiltered = filter === 'all' && !channel && !style

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Library</span>
          <h1 className="display-md reveal reveal-d1">Your films</h1>
        </div>
        <Button variant="primary" icon="plus" onClick={() => go('create')}>New film</Button>
      </div>

      <Banner tone="danger">{error}</Banner>

      {loaded && jobs.finished.length > 0 && (
        <div className="reveal row center gap-10 row--wrap" style={{ marginBottom: 20 }}>
          <Segmented options={filterOpts} value={filter} onChange={setFilter} />
          {(channelOpts.length > 1 || channel) && (
            <FilterSelect value={channel} onChange={(v) => setFilters({ channel: v })}
              options={channelOpts} allLabel="All channels" />
          )}
          {(styleOpts.length > 1 || style) && (
            <FilterSelect value={style} onChange={(v) => setFilters({ style: v })}
              options={styleOpts} allLabel="All styles" />
          )}
        </div>
      )}

      {unfiltered && jobs.resumable.length > 0 && (
        <>
          <div className="label-sm" style={{ marginBottom: 12 }}>Active and unfinished</div>
          <div className="bento" style={{ marginBottom: 28 }}>
            {jobs.resumable.map((j, i) => (
              <Card key={i} span={4} className="reveal">
                <div style={{ fontWeight: 700 }}>{j.label}</div>
                <div className="row center between mt-16">
                  <Chip tone={j.running ? 'info' : 'warn'} dot>{j.running ? 'Rendering' : 'Needs attention'}</Chip>
                  <div className="row gap-6">
                    {j.running ? (
                      <Button variant="ghost" icon="gauge-high" onClick={() => onOpenProgress(j.work_dir)}>View render</Button>
                    ) : (
                      <Button variant="ghost" icon="play" onClick={() => onOpenProgress(j.work_dir)}>Continue</Button>
                    )}
                    {confirmDel === j.work_dir ? (
                      <>
                        <Button variant="danger" icon="trash-can" disabled={busyDel === j.work_dir} onClick={() => del(j.work_dir)}>{busyDel === j.work_dir ? 'Deleting…' : 'Confirm'}</Button>
                        <Button variant="ghost" disabled={busyDel === j.work_dir} onClick={() => setConfirmDel('')}>Cancel</Button>
                      </>
                    ) : (
                      <Button variant="ghost" icon="trash-can" onClick={() => setConfirmDel(j.work_dir)}>Delete</Button>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </>
      )}

      <div className="label-sm" style={{ marginBottom: 12 }}>Finished</div>
      <div className="bento">
        {loaded && jobs.finished.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No finished films yet — create your first one.</p></Card>}
        {loaded && jobs.finished.length > 0 && shown.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No films match this filter.</p></Card>}
        {shown.map((f, i) => (
          <Card key={f.work_dir} span={4} link onClick={() => openCard(f.work_dir)} className={`reveal reveal-d${(i % 4) + 1}`} style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ position: 'relative', aspectRatio: '16/9' }}>
              {f.cover_url
                ? <img src={f.cover_url} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
                : <div className={`gfill g${i % 6}`} style={{ position: 'absolute', inset: 0 }}></div>
              }
              <div style={{ position: 'absolute', top: 12, left: 12 }}>
                {f.published
                  ? <Chip tone="ok" dot>Published</Chip>
                  : f.awaiting_approval
                    ? <Chip tone="warn" dot>Needs approval</Chip>
                    : f.approved
                      ? <Chip tone="info" dot>Approved</Chip>
                      : !f.seen && <Chip tone="info" dot>New</Chip>}
              </div>
              {f.resolution && (
                <div style={{ position: 'absolute', top: 12, right: 12 }}>
                  <Chip>{resBadge(f.resolution)}</Chip>
                </div>
              )}
              <div className="player__play" style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%,-50%)' }}><Icon name="play" /></div>
            </div>
            <div style={{ padding: '14px 18px 16px' }}>
              <div style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{f.label}</div>
              {f.published && f.destinations?.length > 0 && (
                <div className="row gap-6 mt-8 row--wrap">
                  {f.destinations.map((d, k) => {
                    const chip = <Chip tone="ok"><Icon name={d.platform === 'x' ? 'x-twitter' : 'youtube'} brand /> {d.name}</Chip>
                    return d.url
                      ? <a key={k} href={d.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} style={{ textDecoration: 'none' }}>{chip}</a>
                      : <span key={k}>{chip}</span>
                  })}
                </div>
              )}
              <div className="row gap-10 mt-16 row--wrap">
                <Button variant="ghost" icon="sliders" onClick={(e) => { e.stopPropagation(); openCard(f.work_dir) }}>Edit</Button>
                <Button variant="ghost" icon="copy" disabled={busyNew === f.work_dir} onClick={(e) => { e.stopPropagation(); newVersion(f.work_dir) }}>
                  {busyNew === f.work_dir ? 'Copying…' : 'New version'}
                </Button>
                {f.awaiting_approval && (
                  <Button variant="primary" icon="check" disabled={busyApprove === f.work_dir} onClick={(e) => { e.stopPropagation(); approve(f.work_dir) }}>
                    {busyApprove === f.work_dir ? 'Approving…' : 'Approve'}
                  </Button>
                )}
                <Button variant={f.awaiting_approval ? 'ghost' : 'primary'} icon="upload" onClick={(e) => { e.stopPropagation(); go('publish', { publishWorkDir: f.work_dir }) }}>Publish</Button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}
