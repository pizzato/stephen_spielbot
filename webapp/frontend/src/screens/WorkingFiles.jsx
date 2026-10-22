import { useState, useEffect } from 'react'
import { Card, Field, Button, Banner } from '../components.jsx'
import { api } from '../api.js'

function size(bytes = 0) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

export default function WorkingFiles({ workDir, busy, onBusyChange, onClean }) {
  const [data, setData] = useState(null)
  const [policy, setPolicy] = useState('manual')
  const [working, setWorking] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    api.filmWorkingFiles(workDir).then((r) => {
      if (!cancelled) { setData(r); setPolicy(r.policy) }
    }).catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [workDir, busy])

  const run = async (clean) => {
    setWorking(true); onBusyChange(true); setError(''); setMessage('')
    try {
      const r = clean ? await api.cleanFilmWorkingFiles(workDir)
        : await api.setFilmWorkingFilesPolicy(workDir, policy)
      setData(r); setPolicy(r.policy); setConfirm(false)
      const result = r.result || r.last_cleanup
      if (result?.errors?.length) setError(result.errors.join('; '))
      setMessage(clean ? `Freed ${size(r.result.bytes)} from ${r.result.files} files.` : 'Cleanup preference saved.')
      await onClean()
    } catch (e) { setError(e.message) }
    finally { setWorking(false); onBusyChange(false) }
  }

  return (
    <Card span={12} padLg>
      <span className="label-sm">Clean up working files</span>
      <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
        Free space by deleting older versions and upscale caches. Keeps the final video,
        current scene clips and images, selected and source versions needed for editing,
        audio,
        captions, cover and metadata. Deleted versions cannot be restored; upscaling again rebuilds its cache.
      </p>
      <Banner tone="danger">{error}</Banner>
      {message && <Banner tone="ok">{message}</Banner>}
      {data && <>
        <p style={{ fontSize: 13, margin: '12px 0' }}>
          <strong>{size(data.reclaimable.bytes)} reclaimable</strong> · {data.reclaimable.files} files
          {data.reclaimable.groups.map((g) => <span key={g.label}> · {g.label}: {size(g.bytes)}</span>)}
        </p>
        <Field label="Automatic cleanup" hint="Applies only to this video. If the selected milestone has already happened, saving runs cleanup now. Busy films wait until the next automatic check.">
          <select className="select" value={policy} disabled={busy || working} onChange={(e) => setPolicy(e.target.value)}>
            <option value="manual">Off — keep working files</option>
            <option value="after_approval">After final-video approval</option>
            <option value="after_upload">After successful upload to YouTube or X</option>
          </select>
        </Field>
        <div className="row gap-10 row--wrap mt-16">
          <Button disabled={busy || working || policy === data.policy} onClick={() => run(false)}>
            {working ? 'Working…' : policy === 'manual' ? 'Save preference' : 'Enable automatic cleanup'}
          </Button>
          {confirm ? <>
            <Button variant="danger" disabled={busy || working || !!data.blocked_reason} onClick={() => run(true)}>
              {working ? 'Cleaning…' : `Delete ${data.reclaimable.files} working files`}
            </Button>
            <Button disabled={working} onClick={() => setConfirm(false)}>Cancel</Button>
          </> : <Button disabled={busy || working || !!data.blocked_reason || !data.reclaimable.files} onClick={() => setConfirm(true)}>Clean up now…</Button>}
        </div>
        {confirm && <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>Permanently delete the older versions and caches listed above? Your current film and scene assets stay available.</p>}
        {data.blocked_reason && <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>{data.blocked_reason}</p>}
        {data.last_cleanup && <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Last cleanup: {new Date(data.last_cleanup.at * 1000).toLocaleString()} · {size(data.last_cleanup.bytes)} freed
          {data.last_cleanup.errors?.length ? ' · Some files could not be removed.' : ''}
        </p>}
      </>}
    </Card>
  )
}
