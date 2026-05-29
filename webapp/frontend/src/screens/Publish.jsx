import React, { useState, useEffect, useRef } from 'react'
import { Card, Field, Segmented, Button, Icon, Banner } from '../components.jsx'
import { api, fileUrl } from '../api.js'

export default function Publish({ initialWorkDir }) {
  const [opts, setOpts] = useState({ categories: {}, privacy: ['private', 'unlisted', 'public'], finished: [] })
  const [auth, setAuth] = useState({ connected: false })
  const [workDir, setWorkDir] = useState('')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState('22')
  const [privacy, setPrivacy] = useState('private')
  const [coverUrl, setCoverUrl] = useState('')
  const [finalUrl, setFinalUrl] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [confirming, setConfirming] = useState(false)
  const pollRef = useRef(null)

  const refreshAuth = () => api.ytAuthStatus().then(setAuth).catch(() => {})

  useEffect(() => {
    api.ytPostOptions().then((o) => {
      setOpts(o)
      setCategory(o.default_category || '22')
      setPrivacy(o.default_privacy || 'private')
      // Honour a film passed in from the Films screen; otherwise pick the newest.
      const target = (initialWorkDir && o.finished?.some((f) => f.work_dir === initialWorkDir))
        ? initialWorkDir
        : o.finished?.[0]?.work_dir
      if (target) selectFilm(target)
    }).catch((e) => setError(e.message))
    refreshAuth()
    return () => clearInterval(pollRef.current)
  }, [initialWorkDir])

  const selectFilm = async (wd) => {
    setWorkDir(wd); setError(''); setStatus(''); setConfirming(false)
    try {
      const p = await api.ytPostPrefill(wd)
      setTitle(p.title || '')
      setCoverUrl(p.cover_url || '')
      setFinalUrl(p.final_url || '')
    } catch (e) { setError(e.message) }
  }

  const connect = async () => {
    setBusy('auth'); setError('')
    try {
      const { auth_url } = await api.ytAuthStart()
      if (auth_url) window.open(auth_url, '_blank', 'noopener')
      // Poll until the local OAuth redirect completes.
      clearInterval(pollRef.current)
      pollRef.current = setInterval(async () => {
        try {
          const r = await api.ytAuthPoll()
          if (r.status === 'connected' || r.connected) { clearInterval(pollRef.current); setBusy(''); refreshAuth() }
          else if (r.status === 'error') { clearInterval(pollRef.current); setBusy(''); setError(r.error || 'Auth failed') }
        } catch { /* keep polling */ }
      }, 2000)
    } catch (e) { setBusy(''); setError(e.message) }
  }

  const disconnect = async () => { await api.ytDisconnect().catch(() => {}); refreshAuth() }

  const genDescription = async () => {
    setBusy('desc'); setError('')
    try {
      const r = await api.ytDescribe({ work_dir: workDir, title })
      setDescription(r.description || '')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const regenCover = async () => {
    setBusy('cover'); setError('')
    try {
      const r = await api.ytCover({ work_dir: workDir, title })
      setCoverUrl(r.cover_url || '')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const upload = async () => {
    setBusy('upload'); setError(''); setStatus('')
    try {
      const r = await api.ytPost({ work_dir: workDir, title, description, category, privacy })
      setStatus(r.message || 'Uploaded.')
      setConfirming(false)
      refreshAuth()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const canUpload = auth.connected && workDir && title.trim() && finalUrl

  return (
    <div className="bento">
      <Card span={8} padLg className="reveal reveal-d1">
        <div className="row center between row--wrap gap-16">
          <span className="label-sm">Publish a finished film</span>
          {auth.connected
            ? <span className="row center gap-10"><span className="muted" style={{ fontSize: 12.5 }}>{auth.channel_name || 'Connected'}</span><Button variant="ghost" icon="link-slash" onClick={disconnect}>Disconnect</Button></span>
            : <Button variant="primary" icon="youtube" disabled={busy === 'auth'} onClick={connect}>{busy === 'auth' ? 'Waiting for Google…' : 'Connect YouTube'}</Button>}
        </div>

        {!auth.connected && <Banner tone="warn">{auth.error || 'Connect your YouTube channel to publish. Set the client secrets path in Settings if you haven\'t.'}</Banner>}
        <Banner tone="danger">{error}</Banner>
        {status && <Banner tone="ok">{status}</Banner>}

        <div className="stack gap-22 mt-16">
          <Field label="Film">
            <select className="select" value={workDir} onChange={(e) => selectFilm(e.target.value)}>
              {opts.finished.length === 0 && <option value="">No finished films</option>}
              {opts.finished.map((f) => <option key={f.work_dir} value={f.work_dir}>{f.label}</option>)}
            </select>
          </Field>
          <Field label="Title" hint="Max 100 characters.">
            <input className="input" value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label={<span className="row center between"><span>Description</span><button className="btn btn--quiet" style={{ padding: '4px 10px', fontSize: 12 }} disabled={busy === 'desc' || !workDir} onClick={genDescription}><Icon name="wand-magic-sparkles" /> {busy === 'desc' ? 'Writing…' : 'Generate'}</button></span>}>
            <textarea className="textarea" rows={6} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Write a description, or click Generate." />
          </Field>
          <div className="row gap-22 row--wrap">
            <div className="grow">
              <Field label="Category">
                <select className="select" value={category} onChange={(e) => setCategory(e.target.value)}>
                  {Object.entries(opts.categories).map(([name, id]) => <option key={id} value={id}>{name}</option>)}
                </select>
              </Field>
            </div>
            <Field label="Privacy">
              <Segmented value={privacy} onChange={setPrivacy} options={opts.privacy} />
            </Field>
          </div>

          <div className="row center gap-10" style={{ padding: '10px 12px', background: 'var(--warn-soft)', borderRadius: 'var(--r-md)' }}>
            <Icon name="robot" style={{ color: 'var(--warn)' }} />
            <span style={{ fontSize: 12.5, color: 'var(--ink-2)' }}>Uploads are flagged as <strong>synthetic media</strong> per the channel's automated settings.</span>
          </div>

          {confirming ? (
            <div className="row gap-10 center">
              <span className="muted" style={{ fontSize: 13 }}>Upload “{title}” as <strong>{privacy}</strong>?</span>
              <Button variant="primary" icon="youtube" disabled={busy === 'upload'} onClick={upload}>{busy === 'upload' ? 'Uploading…' : 'Confirm upload'}</Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            </div>
          ) : (
            <Button variant="primary" size="lg" icon="youtube" disabled={!canUpload} onClick={() => setConfirming(true)}>Upload to YouTube</Button>
          )}
        </div>
      </Card>

      <div className="col-4 stack gap-16">
        <Card className="reveal reveal-d2">
          <span className="label-sm">Thumbnail</span>
          <div className="mt-16" style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: '16/9' }}>
            {coverUrl
              ? <img src={coverUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
              : <div className="gfill g2" style={{ position: 'absolute', inset: 0 }}></div>}
          </div>
          <Button variant="ghost" block icon="rotate-right" disabled={busy === 'cover' || !workDir} onClick={regenCover}>
            {busy === 'cover' ? 'Painting…' : 'Regenerate cover'}</Button>
        </Card>
        {finalUrl && (
          <Card className="reveal reveal-d3" style={{ padding: 0, overflow: 'hidden' }}>
            <video src={finalUrl} controls style={{ width: '100%', display: 'block', background: '#15171a', aspectRatio: '16/9' }} />
          </Card>
        )}
      </div>
    </div>
  )
}
