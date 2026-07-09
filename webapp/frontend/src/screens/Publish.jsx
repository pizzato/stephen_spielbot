import { useState, useEffect } from 'react'
import { Card, Field, Segmented, Button, Icon, Banner, Check, Chip, RegenLabel, GuidedRegenButton } from '../components.jsx'
import { api } from '../api.js'

function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(Math.round(n))
}
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const fmtHour = (h) => `${h % 12 === 0 ? 12 : h % 12}${h < 12 ? 'a' : 'p'}`

// Best-time-to-post guidance from the timing model (issue #50). A weekday x hour
// heatmap of predicted reach. Renders nothing until a model has been built.
function BestTimesCard({ data }) {
  if (!data?.available || !data.grid?.length) return null
  const max = Math.max(1, ...data.grid.map((g) => g.predicted_views))
  const at = (wd, h) => data.grid.find((g) => g.weekday === wd && g.hour === h)?.predicted_views || 0
  const best = data.best?.[0]
  return (
    <Card className="reveal reveal-d3">
      <span className="label-sm">Best time to post</span>
      {best && <div className="mt-8" style={{ fontSize: 13 }}>Try <strong>{DOW[best.weekday]} {fmtHour(best.hour)}</strong> · ~{fmtNum(best.predicted_views)} views</div>}
      {/* Scrolls horizontally on a narrow phone (minmax keeps cells tappable)
          instead of squeezing 24 hourly columns into unreadable slivers. */}
      <div className="mt-16" style={{ overflowX: 'auto' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '30px repeat(24, minmax(14px, 1fr))', gap: 2 }}>
          {DOW.flatMap((d, wd) => [
            <span key={`l${wd}`} className="muted" style={{ fontSize: 10, alignSelf: 'center' }}>{d}</span>,
            ...Array.from({ length: 24 }, (_, h) => (
              <div key={`${wd}-${h}`} title={`${d} ${fmtHour(h)} · ~${fmtNum(at(wd, h))} views`}
                style={{ aspectRatio: '1', borderRadius: 2, background: 'var(--accent)', opacity: 0.1 + 0.9 * (at(wd, h) / max) }} />
            )),
          ])}
        </div>
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 10 }}>
        {data.reliability && data.reliability !== 'ok' ? 'Rough guidance — ' : 'Advisory — '}
        uploads still post immediately; times in UTC.
      </div>
    </Card>
  )
}

export default function Publish({ initialWorkDir, go }) {
  const [opts, setOpts] = useState({ categories: {}, privacy: ['private', 'unlisted', 'public'], finished: [] })
  // Connected channels (issue #22). The upload goes to `channel` — prefilled
  // from the film's style, overridable here. Connecting lives in Settings.
  const [channels, setChannels] = useState([])
  const [channel, setChannel] = useState('')
  const [workDir, setWorkDir] = useState('')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState('22')
  const [privacy, setPrivacy] = useState('private')
  const [coverUrl, setCoverUrl] = useState('')
  const [finalUrl, setFinalUrl] = useState('')
  const [aspect, setAspect] = useState('16/9')
  const [includeThumbnail, setIncludeThumbnail] = useState(true)
  const [bestTimes, setBestTimes] = useState(null)   // posting-time guidance (issue #50)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [reuploading, setReuploading] = useState(false)
  const [youtubeUrl, setYoutubeUrl] = useState('')
  const [youtubeVideoId, setYoutubeVideoId] = useState('')
  const [uiWorker, setUiWorker] = useState(null)   // cover-worker reservation (issue #98)
  // X (Twitter) posting (issue #107) — a sibling publish destination.
  const [xAccounts, setXAccounts] = useState([])
  const [xAccount, setXAccount] = useState('')
  const [xBusy, setXBusy] = useState(false)
  const [xStatus, setXStatus] = useState('')
  const [xError, setXError] = useState('')
  const [xUrl, setXUrl] = useState('')
  // Which destinations to publish to (issue #107). A box is only acted on when
  // that platform also has a connected channel/account (see willYouTube/willX).
  const [dest, setDest] = useState({ youtube: true, x: true })

  const refreshChannels = () => api.ytChannels().then((r) => setChannels(r.channels || [])).catch(() => {})
  const refreshXAccounts = () => api.xAccounts().then((r) => {
    const accs = r.accounts || []
    setXAccounts(accs)
    setXAccount((a) => a || accs.find((x) => x.connected)?.id || accs[0]?.id || '')
  }).catch(() => {})

  // Poll the UI-worker reservation so we can tell the user, next to the cover
  // button, when a render worker will be free for a regenerate (issue #98).
  useEffect(() => {
    let alive = true
    const tick = () => api.uiWorker().then((u) => { if (alive) setUiWorker(u) }).catch(() => {})
    tick()
    const t = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

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
    refreshChannels()
    refreshXAccounts()
  }, [initialWorkDir])

  const selectFilm = async (wd) => {
    setWorkDir(wd); setError(''); setStatus(''); setConfirming(false); setReuploading(false); setYoutubeUrl(''); setYoutubeVideoId('')
    setXStatus(''); setXError(''); setXUrl('')
    try {
      const p = await api.ytPostPrefill(wd)
      setTitle(p.title || '')
      setDescription(p.description || '')
      setCoverUrl(p.cover_url || '')
      setFinalUrl(p.final_url || '')
      setYoutubeUrl(p.youtube_url || '')
      setYoutubeVideoId(p.youtube_video_id || '')
      setChannel(p.channel || '')   // the film's style decides the target channel
      setCategory(p.category || '22')   // …and that channel's default video category
      // The style decides the X account too; with none, default the X box off.
      if (p.x_account) setXAccount(p.x_account)
      setDest({ youtube: true, x: !!p.x_account })
      setAspect(p.vid_width && p.vid_height ? `${p.vid_width}/${p.vid_height}` : '16/9')
      setIncludeThumbnail(p.include_thumbnail_default !== false)
      setBestTimes(null)
      // A portrait film is a Short — the timing model weighs that differently.
      const isShort = !!(p.vid_width && p.vid_height && Number(p.vid_height) > Number(p.vid_width))
      api.engagementBestTimes({ title: p.title || '', description: p.description || '', is_short: isShort, channel: p.channel || '' })
        .then(setBestTimes).catch(() => {})
    } catch (e) { setError(e.message) }
  }

  const genDescription = async () => {
    setBusy('desc'); setError('')
    try {
      const r = await api.ytDescribe({ work_dir: workDir, title })
      setDescription(r.description || '')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const regenTitle = async (instruction = '') => {
    setBusy('title'); setError('')
    try {
      const r = await api.ytPostTitle(workDir, title, instruction)
      setTitle(r.title || '')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const regenCover = async (instruction = '') => {
    setBusy('cover'); setError('')
    let pollTimer = null
    try {
      const { task_id: tid } = await api.ytCover({ work_dir: workDir, title, instruction })
      await new Promise((resolve, reject) => {
        const check = async () => {
          try {
            const s = await api.ytCoverStatus(tid)
            if (s.status === 'succeeded') { setCoverUrl(s.cover_url || ''); resolve() }
            else if (s.status === 'failed_terminal') reject(new Error(s.error || 'Cover generation failed'))
            else pollTimer = setTimeout(check, 2000)
          } catch (e) { reject(e) }
        }
        check()
      })
    } catch (e) { setError(e.message) } finally {
      clearTimeout(pollTimer)
      setBusy('')
    }
  }

  // Push the current cover to the already-published video's thumbnail.
  const updateThumbnail = async () => {
    setBusy('thumb'); setError(''); setStatus('')
    try {
      await api.ytThumbnail({ work_dir: workDir, video_id: youtubeVideoId })
      setStatus('Thumbnail updated on YouTube.')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  // Upload to YouTube and resolve once done (so a combined publish can wait for
  // the link before posting to X). Throws on failure.
  const uploadYouTube = async () => {
    setStatus('Uploading to YouTube…')
    const { task_id } = await api.ytPost({ work_dir: workDir, title, description, category, privacy, include_thumbnail: includeThumbnail, channel: chan?.id || '' })
    let pollTimer = null
    try {
      await new Promise((resolve, reject) => {
        const check = async () => {
          try {
            const s = await api.ytPostStatus(task_id)
            if (s.status === 'done') {
              setStatus(s.message || 'Uploaded to YouTube.')
              setYoutubeUrl(s.url || '')
              setYoutubeVideoId(s.video_id || '')
              refreshChannels()
              resolve()
            } else if (s.status === 'error') {
              reject(new Error(s.error || 'Upload failed'))
            } else {
              setStatus('Uploading to YouTube… (this can take a few minutes)')
              pollTimer = setTimeout(check, 4000)
            }
          } catch (e) { reject(e) }
        }
        check()
      })
    } finally { clearTimeout(pollTimer) }
  }

  // Publish to the selected destinations. YouTube goes first so a non-Premium X
  // post can fall back to the fresh YouTube link (the backend reads it from the
  // film's job meta, written by the YouTube upload).
  const publishAll = async () => {
    setBusy('publish'); setError('')
    try {
      if (willYouTube) await uploadYouTube()
      if (willX) await postToX()
      setConfirming(false); setReuploading(false)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  // Switching the publish channel pulls in that channel's default category.
  const onChannelChange = (id) => {
    setChannel(id)
    const c = channels.find((x) => x.id === id)
    setCategory(c?.video_category || opts.default_category || '22')
  }

  // The channel this upload goes to — prefilled from the film's style, overridable.
  const chan = channels.find((c) => c.id === channel) || channels[0]
  // The X account this post goes to. Non-Premium accounts can't take a long
  // video — the backend posts the YouTube link instead (or warns if none).
  const xacc = xAccounts.find((a) => a.id === xAccount) || xAccounts[0]

  // Resolved publish targets: a destination only fires when it's both ticked and
  // has a connected channel/account.
  const ytAvailable = !!chan?.connected
  const xAvailable = !!xacc?.connected
  const willYouTube = dest.youtube && ytAvailable
  const willX = dest.x && xAvailable
  const canPublish = !!(workDir && title.trim() && finalUrl && (willYouTube || willX))

  const postToX = async () => {
    setXBusy(true); setXError(''); setXStatus('Posting to X…'); setXUrl('')
    let pollTimer = null
    try {
      // X posts the description body (before the style's sign-off), not the title.
      const { task_id } = await api.xPost({ work_dir: workDir, title, text: description, account: xacc?.id || '' })
      await new Promise((resolve, reject) => {
        const check = async () => {
          try {
            const s = await api.xPostStatus(task_id)
            if (s.status === 'done') { setXStatus(s.message || 'Posted to X.'); setXUrl(s.url || ''); resolve() }
            else if (s.status === 'warning') { setXStatus(s.message || 'Not posted to X.'); resolve() }
            else if (s.status === 'error') { reject(new Error(s.error || 'X post failed')) }
            else { setXStatus('Posting to X… (this can take a minute)'); pollTimer = setTimeout(check, 4000) }
          } catch (e) { reject(e) }
        }
        check()
      })
    } catch (e) { setXError(e.message) } finally { clearTimeout(pollTimer); setXBusy(false) }
  }

  return (
    <div className="bento">
      <Card span={8} padLg className="reveal reveal-d1">
        <div className="row center between row--wrap gap-16">
          <span className="row center gap-10"><span className="label-sm">Publish a finished film</span>{go && <Button variant="ghost" icon="film" disabled={!workDir} onClick={() => go('editfilm', { workDir })}>Edit</Button>}</span>
        </div>

        {channels.length === 0 && xAccounts.length === 0 && (
          <Banner tone="warn">No channels or accounts connected — add one in Settings → YouTube or Settings → X.</Banner>)}
        <Banner tone="danger">{error}</Banner>
        {status && <Banner tone="ok">{status}</Banner>}

        <div className="stack gap-22 mt-16">
          <Field label="Film">
            <select className="select" value={workDir} onChange={(e) => selectFilm(e.target.value)}>
              {opts.finished.length === 0 && <option value="">No finished films</option>}
              {opts.finished.map((f) => <option key={f.work_dir} value={f.work_dir}>{f.label}</option>)}
            </select>
          </Field>
          <Field label="Publish to">
            <div className="stack gap-10">
              <div className="row center gap-10 row--wrap">
                <Check checked={dest.youtube} onChange={(v) => setDest((d) => ({ ...d, youtube: v }))} label="YouTube" />
                {dest.youtube && channels.length > 0 && (<>
                  <select className="select" value={chan?.id || ''} onChange={(e) => onChannelChange(e.target.value)} style={{ maxWidth: 220 }}>
                    {channels.map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
                  </select>
                  {chan?.connected ? <Chip tone="ok" dot>connected</Chip> : <Chip tone="danger" dot>not connected</Chip>}
                </>)}
                {dest.youtube && channels.length === 0 && <span className="muted" style={{ fontSize: 12 }}>No channel connected (Settings → YouTube)</span>}
              </div>
              <div className="row center gap-10 row--wrap">
                <Check checked={dest.x} onChange={(v) => setDest((d) => ({ ...d, x: v }))} label="X" />
                {dest.x && xAccounts.length > 0 && (<>
                  <select className="select" value={xacc?.id || ''} onChange={(e) => setXAccount(e.target.value)} style={{ maxWidth: 220 }}>
                    {xAccounts.map((a) => <option key={a.id} value={a.id}>{a.name ? `@${a.name}` : a.id}</option>)}
                  </select>
                  {xacc?.connected ? <Chip tone="ok" dot>connected</Chip> : <Chip tone="danger" dot>not connected</Chip>}
                  {xacc?.premium ? <Chip tone="accent">Premium</Chip> : null}
                </>)}
                {dest.x && xAccounts.length === 0 && <span className="muted" style={{ fontSize: 12 }}>No account connected (Settings → X)</span>}
              </div>
              {willX && <div className="muted" style={{ fontSize: 11.5 }}>X posts the video description (the part before the style’s sign-off), not the title.</div>}
              {willX && !xacc?.premium && (
                <div className="muted" style={{ fontSize: 11.5 }}>
                  X is non-Premium: videos over 2m20s post the YouTube link instead{(willYouTube || youtubeUrl) ? '' : ' — enable YouTube too, or it won’t post'}.
                </div>
              )}
            </div>
          </Field>
          <Field label={<RegenLabel busy={busy === 'title'} disabled={!workDir} onRegen={regenTitle} chips={['Shorter', 'Punchier', 'More literal']}>Title</RegenLabel>} hint="Max 100 characters (YouTube title).">
            <input className="input" value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          {(dest.youtube || dest.x) && (
            <Field label={<span className="row center between"><span>Description</span><button className="btn btn--quiet" style={{ padding: '4px 10px', fontSize: 12 }} disabled={busy === 'desc' || !workDir} onClick={genDescription}><Icon name="wand-magic-sparkles" /> {busy === 'desc' ? 'Writing…' : 'Generate'}</button></span>}
              hint={dest.youtube
                ? (dest.x ? 'YouTube description. X posts this minus the style’s sign-off.' : 'YouTube description.')
                : 'Posted to X (minus the style’s sign-off) — not the title.'}>
              <textarea className="textarea" rows={6} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Write a description, or click Generate." />
            </Field>
          )}
          {dest.youtube && (
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
          )}

          <div className="row center gap-10" style={{ padding: '10px 12px', background: 'var(--warn-soft)', borderRadius: 'var(--r-md)' }}>
            <Icon name="robot" style={{ color: 'var(--warn)' }} />
            <span style={{ fontSize: 12.5, color: 'var(--ink-2)' }}>Posts are flagged as <strong>synthetic media</strong> per each platform's automated settings.</span>
          </div>

          {confirming ? (
            <div className="row gap-10 center row--wrap">
              <span className="muted" style={{ fontSize: 13 }}>
                Publish "{title}" to {[willYouTube && `YouTube (${privacy})`, willX && `X (${xacc?.name ? '@' + xacc.name : 'account'})`].filter(Boolean).join(' + ')}?
              </span>
              <Button variant="primary" icon="upload" disabled={busy === 'publish'} onClick={publishAll}>{busy === 'publish' ? 'Publishing…' : 'Confirm'}</Button>
              <Button variant="ghost" onClick={() => { setConfirming(false); setReuploading(false) }}>Cancel</Button>
            </div>
          ) : (
            <Button variant="primary" size="lg" icon="upload" disabled={!canPublish || busy === 'publish'} onClick={() => setConfirming(true)}>
              {busy === 'publish' ? 'Publishing…' : ((youtubeUrl || xUrl) ? 'Publish again' : 'Publish')}
            </Button>
          )}
          {(youtubeUrl || xUrl) && (
            <div className="row center gap-10 row--wrap">
              {youtubeUrl && <a className="btn btn--ghost" href={youtubeUrl} target="_blank" rel="noreferrer"><Icon name="youtube" brand /> View on YouTube</a>}
              {xUrl && <a className="btn btn--ghost" href={xUrl} target="_blank" rel="noreferrer"><Icon name="x-twitter" brand /> View on X</a>}
            </div>
          )}
          {xStatus && <Banner tone="ok">{xStatus}</Banner>}
          {xError && <Banner tone="danger">{xError}</Banner>}
        </div>
      </Card>

      <div className="col-4 stack gap-16">
        <Card className="reveal reveal-d2">
          <span className="label-sm">Thumbnail</span>
          <div className="mt-16" style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: aspect, maxHeight: 360, margin: '16px auto 0', opacity: includeThumbnail ? 1 : 0.5 }}>
            {coverUrl
              ? <img src={coverUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', background: '#15171a' }} />
              : <div className="gfill g2" style={{ position: 'absolute', inset: 0 }}></div>}
          </div>
          <div className="mt-16">
            <Check checked={includeThumbnail} onChange={setIncludeThumbnail}
              label="Upload thumbnail to YouTube" />
          </div>
          <GuidedRegenButton block variant="ghost" icon="rotate-right"
            label="Regenerate cover"
            busyLabel={uiWorker?.active && !uiWorker.available && uiWorker.eta_text
              ? `Queued — worker free in ${uiWorker.eta_text}…`
              : 'Generating cover…'}
            busy={busy === 'cover'} disabled={busy === 'cover' || !workDir}
            onRegen={regenCover} chips={['Bolder', 'Simpler', 'More dramatic']} />
          {busy !== 'cover' && uiWorker?.active && !uiWorker.available && uiWorker.eta_text && (
            <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>
              Render busy — a worker will be free for covers in {uiWorker.eta_text}.
            </div>
          )}
          {youtubeUrl && (
            <Button variant="ghost" block icon="image" disabled={busy === 'thumb' || !coverUrl} onClick={updateThumbnail}>
              {busy === 'thumb' ? 'Updating…' : 'Update thumbnail on YouTube'}</Button>
          )}
        </Card>
        {finalUrl && (
          <Card className="reveal reveal-d3" style={{ padding: 0, overflow: 'hidden' }}>
            <video src={finalUrl} controls style={{ width: '100%', display: 'block', background: '#15171a', aspectRatio: aspect, maxHeight: 360, objectFit: 'contain' }} />
          </Card>
        )}
        <BestTimesCard data={bestTimes} />
      </div>
    </div>
  )
}
