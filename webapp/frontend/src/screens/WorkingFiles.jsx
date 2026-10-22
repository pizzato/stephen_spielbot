import { useState, useEffect } from 'react'
import { Card, Button, Banner } from '../components.jsx'
import { api, fileUrl } from '../api.js'

function size(bytes = 0) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

function FileGroups({ groups, downloads = false }) {
  return groups.map((g) => <details key={g.label} style={{ marginTop: 8, fontSize: 13 }}>
    <summary>{g.label} · {g.files} files · {size(g.bytes)}</summary>
    <ul style={{ maxHeight: 220, overflow: 'auto', overflowWrap: 'anywhere' }}>
      {g.items.map((f) => <li key={f.name}>
        {downloads ? <a href={fileUrl(f.path)} download>{f.name}</a> : f.name} · {size(f.bytes)}
      </li>)}
    </ul>
  </details>)
}

export default function WorkingFiles({ workDir, busy, onBusyChange, onClean }) {
  const [data, setData] = useState(null)
  const [working, setWorking] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    api.filmWorkingFiles(workDir).then((r) => {
      if (!cancelled) setData(r)
    }).catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [workDir, busy])

  const run = async () => {
    setWorking(true); onBusyChange(true); setError(''); setMessage('')
    try {
      const r = await api.cleanFilmWorkingFiles(workDir)
      setData(r); setConfirm(false)
      const result = r.result
      if (result?.errors?.length) setError(result.errors.join('; '))
      setMessage(`Removed ${size(result.bytes)} from ${result.files} working files.`)
      await onClean()
    } catch (e) { setError(e.message) }
    finally { setWorking(false); onBusyChange(false) }
  }

  return (
    <Card span={12} padLg>
      <span className="label-sm">Clean up working files</span>
      <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
        Keep the final video and reusable assets: scripts, closed captions, music, narration,
        other audio, current images, covers and references. Remove scene video clips, intermediate
        assembled videos, video versions, older image versions and upscale caches.
        Automatic cleanup is configured in Settings → Styles.
      </p>
      <Banner tone="danger">{error}</Banner>
      {message && <Banner tone="ok">{message}</Banner>}
      {data && <>
        <p style={{ fontSize: 13, margin: '12px 0' }}>
          <strong>{size(data.reclaimable.bytes)} to remove</strong> · {data.reclaimable.files} working files
        </p>
        <details>
          <summary>Files to remove</summary>
          <FileGroups groups={data.reclaimable.groups} />
        </details>
        <details style={{ marginTop: 10 }}>
          <summary>Files to keep / download</summary>
          <FileGroups groups={data.reclaimable.kept.groups} downloads />
        </details>
        <p className="muted" style={{ fontSize: 13, marginTop: 12 }}>
          Deleted clips cannot be restored. The final stays playable and publishable, but scene editing,
          remixing and reassembly require rendering a new copy from the saved script.
          Any speech held only in a current scene video is saved as separate audio before removal.
        </p>
        <div className="row gap-10 row--wrap mt-16">
          {confirm ? <>
            <Button variant="danger" disabled={busy || working || !!data.blocked_reason} onClick={run}>
              {working ? 'Cleaning…' : `Delete ${data.reclaimable.files} working files`}
            </Button>
            <Button disabled={working} onClick={() => setConfirm(false)}>Cancel</Button>
          </> : <Button disabled={busy || working || !!data.blocked_reason || !data.reclaimable.files} onClick={() => setConfirm(true)}>Clean up now…</Button>}
        </div>
        {confirm && <Banner tone="warn">Permanently delete the working files listed above and keep the final video with its reusable assets?</Banner>}
        {data.blocked_reason && <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>{data.blocked_reason}</p>}
        {data.last_cleanup && <details style={{ fontSize: 12, marginTop: 10 }}>
          <summary>Last cleanup: {new Date(data.last_cleanup.at * 1000).toLocaleString()} · {data.last_cleanup.files} files · {size(data.last_cleanup.bytes)} removed</summary>
          {data.last_cleanup.removed
            ? <ul style={{ maxHeight: 220, overflow: 'auto', overflowWrap: 'anywhere' }}>{data.last_cleanup.removed.map((f) => <li key={f.name}>{f.name} · {size(f.bytes)}</li>)}</ul>
            : <p>This older cleanup recorded totals only; filenames are unavailable.</p>}
          {data.last_cleanup.errors?.map((e) => <p key={e}>{e}</p>)}
        </details>}
      </>}
    </Card>
  )
}
