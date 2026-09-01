import { useState, useEffect } from 'react'
import { Card, Chip, Button, Banner } from '../components.jsx'
import { api } from '../api.js'
import { analyticsCache } from './Analytics.jsx'

// "Manage Videos" tab on Channel Analytics: the same published-video catalogue
// as the Analytics tab, but with checkboxes and bulk actions — change
// visibility, add to a playlist, or delete (with confirmation). YouTube only:
// the X API offers no equivalent bulk endpoints.

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

const PRIVACY_TONES = { public: 'ok', unlisted: 'warn', private: 'neutral' }
const COLS = [
  { label: '' },
  { label: '' },
  { label: 'Title', key: 'title', align: 'left' },
  { label: 'Visibility', key: 'privacy', align: 'left' },
  { label: 'Duration' },
  { label: 'Views', key: 'view_count' },
  { label: 'Likes', key: 'like_count' },
  { label: 'Comments', key: 'comment_count' },
  { label: 'Published', key: 'published_at' },
]

function sortVideos(videos, sort) {
  if (!sort.key) return videos
  const dir = sort.dir === 'asc' ? 1 : -1
  const val = (v) => v[sort.key] == null ? -1 : v[sort.key]
  return [...videos].sort((a, b) => { const av = val(a), bv = val(b); return (av < bv ? -1 : av > bv ? 1 : 0) * dir })
}

function DeleteConfirm({ videos, busy, onConfirm, onClose }) {
  const shown = videos.slice(0, 8)
  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 1100, background: 'rgba(0,0,0,.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}
      onClick={busy ? undefined : onClose}>
      <div className="card" style={{ maxWidth: 460, width: '100%', padding: 24 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 8 }}>Delete {videos.length} video{videos.length === 1 ? '' : 's'} from YouTube?</div>
        <p style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 12 }}>
          This permanently deletes the video{videos.length === 1 ? '' : 's'} — views, likes and comments included. It cannot be undone.
        </p>
        <ul style={{ fontSize: 13, margin: '0 0 16px 18px', color: 'var(--ink-3)' }}>
          {shown.map((v) => <li key={v.video_id} style={{ marginBottom: 2 }}>{v.title}</li>)}
          {videos.length > shown.length && <li>…and {videos.length - shown.length} more</li>}
        </ul>
        <div className="row" style={{ gap: 10, justifyContent: 'flex-end' }}>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="danger" icon="trash" disabled={busy} onClick={onConfirm}>
            {busy ? 'Deleting…' : `Delete ${videos.length} video${videos.length === 1 ? '' : 's'}`}
          </Button>
        </div>
      </div>
    </div>
  )
}

export default function ManageVideos() {
  const [channels, setChannels] = useState([])
  const [channel, setChannel] = useState('')
  const [videos, setVideos] = useState(null)      // null = loading
  const [selected, setSelected] = useState(new Set())
  const [sort, setSort] = useState({ key: 'published_at', dir: 'desc' })
  const [playlists, setPlaylists] = useState([])
  const [privacy, setPrivacy] = useState('private')
  const [playlistId, setPlaylistId] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [notice, setNotice] = useState(null)      // { tone, text }

  useEffect(() => {
    api.ytChannels().then((r) => {
      const list = r.channels || []
      setChannels(list); setChannel(list[0]?.id || '')
    }).catch(() => setChannels([]))
  }, [])

  useEffect(() => {
    if (channel === '' && channels.length) return
    setVideos(null); setSelected(new Set()); setNotice(null)
    const cached = analyticsCache.youtube[channel]
    const load = cached ? Promise.resolve(cached) : api.ytAnalytics(channel, false)
    load.then((d) => {
      if (!cached) analyticsCache.youtube[channel] = d
      setVideos(d.videos || [])
    }).catch((e) => { setVideos([]); setNotice({ tone: 'danger', text: e.message }) })
    api.ytPlaylists(channel).then((r) => {
      const pls = r.playlists || []
      setPlaylists(pls); setPlaylistId(pls[0]?.id || '')
    }).catch(() => setPlaylists([]))
  }, [channel, channels])

  const toggle = (id) => setSelected((s) => {
    const next = new Set(s)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const shown = sortVideos(videos || [], sort)
  const allSelected = shown.length > 0 && shown.every((v) => selected.has(v.video_id))
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(shown.map((v) => v.video_id)))

  const runBulk = async (action) => {
    const ids = [...selected]
    setBusy(true); setNotice(null)
    try {
      const body = { channel, action, video_ids: ids, privacy, playlist_id: playlistId }
      const res = await api.ytVideosBulk(body)
      const okIds = new Set(res.results.filter((r) => r.success).map((r) => r.video_id))
      // Reflect the change locally and drop the cached snapshot — the backend
      // patched its persisted copy, so the Analytics tab refetches it cheaply.
      delete analyticsCache.youtube[channel]
      if (action === 'delete') {
        setVideos((vs) => vs.filter((v) => !okIds.has(v.video_id)))
      } else if (action === 'privacy') {
        setVideos((vs) => vs.map((v) => okIds.has(v.video_id) ? { ...v, privacy } : v))
      }
      setSelected(new Set())
      const verb = action === 'delete' ? 'Deleted' : action === 'privacy' ? `Set to ${privacy}` : 'Added to playlist'
      if (res.failed > 0) {
        const firstErr = res.results.find((r) => !r.success)?.error || ''
        setNotice({ tone: 'danger', text: `${verb}: ${res.succeeded} succeeded, ${res.failed} failed. ${firstErr}` })
      } else {
        setNotice({ tone: 'ok', text: `${verb}: ${res.succeeded} video${res.succeeded === 1 ? '' : 's'}.` })
      }
    } catch (e) {
      setNotice({ tone: 'danger', text: e.message })
    } finally {
      setBusy(false); setConfirming(false)
    }
  }

  const none = selected.size === 0
  return (
    <div>
      <div className="row center reveal reveal-d1" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
        <Chip tone="neutral">YouTube</Chip>
        {channels.length > 1 && (
          <select className="select" value={channel} onChange={(e) => setChannel(e.target.value)} style={{ maxWidth: 220 }}>
            {channels.map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
          </select>
        )}
        {channels.length === 1 && <Chip tone="ok" dot>{channels[0].name || channels[0].id}</Chip>}
      </div>

      {notice && <Banner tone={notice.tone === 'ok' ? 'ok' : 'danger'}>{notice.text}</Banner>}

      <div className="bento">
        <Card span={12} className="reveal reveal-d1" style={{ minWidth: 0 }}>
          <div className="row center" style={{ gap: 12, flexWrap: 'wrap', marginBottom: 14 }}>
            <Chip tone={none ? 'neutral' : 'ok'}>{selected.size} selected</Chip>
            <div className="row center" style={{ gap: 6 }}>
              <select className="select" value={privacy} onChange={(e) => setPrivacy(e.target.value)} disabled={busy} style={{ width: 110 }}>
                {['private', 'unlisted', 'public'].map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <Button size="sm" icon="eye" disabled={none || busy} onClick={() => runBulk('privacy')}>Set visibility</Button>
            </div>
            <div className="row center" style={{ gap: 6 }}>
              <select className="select" value={playlistId} onChange={(e) => setPlaylistId(e.target.value)} disabled={busy || !playlists.length} style={{ maxWidth: 200 }}>
                {playlists.length
                  ? playlists.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)
                  : <option value="">No playlists</option>}
              </select>
              <Button size="sm" icon="list" disabled={none || busy || !playlistId} onClick={() => runBulk('playlist')}>Add to playlist</Button>
            </div>
            <Button size="sm" variant="danger" icon="trash" disabled={none || busy} onClick={() => setConfirming(true)}>Delete…</Button>
          </div>

          {videos === null && <p className="muted" style={{ fontSize: 13 }}>Loading videos…</p>}
          {videos !== null && videos.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No videos found. Connect YouTube to manage your uploads.</p>}
          {videos !== null && videos.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', minWidth: 760, borderCollapse: 'collapse', fontSize: 13 }}>
                <thead><tr style={{ borderBottom: '1px solid var(--line)' }}>
                  {COLS.map((c, i) => (
                    <th key={i}
                      onClick={c.key ? () => setSort((s) => ({ key: c.key, dir: s.key === c.key && s.dir === 'desc' ? 'asc' : 'desc' })) : undefined}
                      style={{ textAlign: c.align || (i <= 2 ? 'left' : 'right'), padding: '6px 10px', fontWeight: 600,
                               color: sort.key === c.key ? 'var(--ink)' : 'var(--ink-3)', whiteSpace: 'nowrap',
                               cursor: c.key ? 'pointer' : 'default', userSelect: 'none' }}>
                      {i === 0
                        ? <input type="checkbox" checked={allSelected} onChange={toggleAll} disabled={busy} />
                        : <>{c.label}{sort.key === c.key ? (sort.dir === 'desc' ? ' ↓' : ' ↑') : ''}</>}
                    </th>
                  ))}
                </tr></thead>
                <tbody>
                  {shown.map((v) => (
                    <tr key={v.video_id} style={{ borderBottom: '1px solid var(--line)', background: selected.has(v.video_id) ? 'var(--paper-2)' : undefined, cursor: 'pointer' }}
                      onClick={() => !busy && toggle(v.video_id)}>
                      <td style={{ padding: '8px 10px' }}>
                        <input type="checkbox" checked={selected.has(v.video_id)} disabled={busy}
                          onChange={() => toggle(v.video_id)} onClick={(e) => e.stopPropagation()} />
                      </td>
                      <td style={{ padding: '8px 10px 8px 0' }}>{v.thumbnail_url ? <img src={v.thumbnail_url} alt="" style={{ width: 48, height: 27, objectFit: 'cover', borderRadius: 4, display: 'block' }} /> : <div style={{ width: 48, height: 27, background: 'var(--surface-2)', borderRadius: 4 }} />}</td>
                      <td style={{ padding: '8px 10px', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={v.title}>
                        <a href={`https://www.youtube.com/watch?v=${v.video_id}`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} style={{ color: 'inherit', textDecoration: 'none', fontWeight: 500 }}>{v.title}</a>
                      </td>
                      <td style={{ padding: '8px 10px' }}><Chip tone={PRIVACY_TONES[v.privacy] || 'neutral'}>{v.privacy || '—'}</Chip></td>
                      <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{v.duration || '—'}</td>
                      <td style={{ textAlign: 'right', padding: '8px 10px', fontWeight: 600 }}>{fmtNum(v.view_count)}</td>
                      <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.like_count)}</td>
                      <td style={{ textAlign: 'right', padding: '8px 10px', color: 'var(--ink-3)' }}>{fmtNum(v.comment_count)}</td>
                      <td style={{ textAlign: 'right', padding: '8px 0 8px 10px', color: 'var(--ink-3)', whiteSpace: 'nowrap' }}>{fmtDate(v.published_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {confirming && (
        <DeleteConfirm
          videos={shown.filter((v) => selected.has(v.video_id))}
          busy={busy}
          onConfirm={() => runBulk('delete')}
          onClose={() => setConfirming(false)}
        />
      )}
    </div>
  )
}
