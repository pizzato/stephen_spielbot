import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Card, Field, Button, Icon, Banner, GuidedRegenButton, CatalogueRefCard } from '../components.jsx'

const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = reject
  r.readAsDataURL(file)
})

// Locations, wardrobe, and free-form image/video references: the pictures that
// stop a performance film drifting between scenes. Words alone don't hold a
// room, an outfit or a prop still — these ride the same <Picture N> slots as
// the cast, after it. A video reference contributes its extracted frame.

const KIND_META = {
  location: { label: 'Location', icon: 'location-dot' },
  wardrobe: { label: 'Wardrobe', icon: 'shirt' },
  image: { label: 'Image', icon: 'image' },
  video: { label: 'Video', icon: 'film' },
  // A soundtrack ARTIFACT: the whole track is pinned into the H3 takes of the
  // scenes it applies to, and the picture is generated to match it.
  audio: { label: 'Soundtrack', icon: 'music' },
}

function VisualCard({ v, jobId, sceneIds, castNames, onChanged, onError }) {
  const [busy, setBusy] = useState('')
  const run = async (what, fn) => {
    setBusy(what)
    try { await fn(); await onChanged() } catch (e) { onError(e.message) } finally { setBusy('') }
  }
  const patch = (body) => run('save', () => api.updateVisual(jobId, v.id, body))
  const everyScene = !v.scenes || v.scenes.length === 0

  const km = KIND_META[v.kind] || KIND_META.location
  const [urlDraft, setUrlDraft] = useState('')
  const pasteImage = () => run('img', async () => {
    const items = await navigator.clipboard.read()
    for (const item of items) {
      const type = item.types.find((t) => t.startsWith('image/'))
      if (type) {
        const blob = await item.getType(type)
        const data = await fileToDataUrl(blob)
        return api.uploadVisualImage(jobId, v.id, `pasted.${type.split('/')[1] || 'png'}`, data)
      }
    }
    throw new Error('No image on the clipboard.')
  })

  return (
    <Card span={4} className="stack gap-12">
      <div className="row between center">
        <span className="label-sm">{km.label}</span>
        <Button variant="quiet" size="sm" icon="trash-can" disabled={!!busy}
          onClick={() => run('del', () => api.deleteVisual(jobId, v.id))} />
      </div>

      {v.kind === 'audio'
        ? <div className="stack gap-8" style={{ padding: '18px 0' }}>
            {v.audio_url
              ? <audio controls src={v.audio_url} style={{ width: '100%' }} />
              : <div style={{ width: '100%', padding: '22px 0', borderRadius: 10, display: 'flex',
                              alignItems: 'center', justifyContent: 'center',
                              background: 'var(--well, rgba(127,127,127,.10))',
                              border: '1px dashed var(--line, #ccc)' }}>
                  <Icon name="music" style={{ color: 'var(--ink-3)', fontSize: 26 }} />
                </div>}
          </div>
        : v.image_url
        ? <img src={v.image_url} alt={v.name}
            style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', borderRadius: 10 }} />
        : <div style={{ width: '100%', aspectRatio: '1', borderRadius: 10, display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'var(--well, rgba(127,127,127,.10))',
                        border: '1px dashed var(--line, #ccc)' }}>
            <Icon name={km.icon} style={{ color: 'var(--ink-3)', fontSize: 26 }} />
          </div>}

      <Field label="Name">
        <input className="input" defaultValue={v.name} disabled={!!busy}
          onBlur={(e) => e.target.value !== v.name && patch({ name: e.target.value })} />
      </Field>
      <Field label="Description"
        hint={v.kind === 'audio'
          ? 'What this track is, for your own reference. The takes it applies to are generated AGAINST it — mouths and movement follow the sound — and keep it as their audio.'
          : v.kind === 'image' || v.kind === 'video'
          ? 'WHAT this is — the model is told to match exactly this where it appears.'
          : 'What the image should show. Painted with no people in it.'}>
        <textarea className="textarea" rows={2} defaultValue={v.description} disabled={!!busy}
          onBlur={(e) => e.target.value !== v.description && patch({ description: e.target.value })} />
      </Field>

      {(v.kind === 'image' || v.kind === 'video' || v.kind === 'audio') && (
        <Field label="How it's used"
          hint={v.kind === 'audio'
            ? 'Optional — what the performers do with this track, e.g. "the characters dance to this music". Goes into the take’s prompt.'
            : 'Optional — what the model should DO with it, e.g. "the characters copy this dance’s movements". Replaces the default "match it exactly".'}>
          <textarea className="textarea" rows={2} defaultValue={v.usage} disabled={!!busy}
            onBlur={(e) => e.target.value !== v.usage && patch({ usage: e.target.value })} />
        </Field>
      )}

      {v.kind === 'wardrobe' && (
        <Field label="Worn by">
          <select className="select" value={v.character || ''} disabled={!!busy}
            onChange={(e) => patch({ character: e.target.value })}>
            <option value="">Anyone in the scene</option>
            {castNames.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </Field>
      )}

      <Field label="Used in" hint={everyScene ? 'Every scene' : `${v.scenes.length} scene(s)`}>
        <div className="row gap-6 row--wrap">
          <Button size="sm" variant={everyScene ? 'primary' : 'ghost'} disabled={!!busy}
            onClick={() => patch({ scenes: [] })}>All</Button>
          {sceneIds.map((sid) => (
            <Button key={sid} size="sm" disabled={!!busy}
              variant={!everyScene && v.scenes.includes(sid) ? 'primary' : 'ghost'}
              onClick={() => {
                const next = everyScene ? [sid]
                  : v.scenes.includes(sid) ? v.scenes.filter((x) => x !== sid)
                    : [...v.scenes, sid].sort((a, b) => a - b)
                patch({ scenes: next.length ? next : [] })
              }}>{sid}</Button>
          ))}
        </div>
      </Field>

      <div className="stack gap-6">
        {/* A real photo of the actual room or garment beats anything painted,
            so uploading is a first-class option, not a fallback. */}
        <label className="btn btn--ghost btn--sm btn--block" style={{ cursor: busy ? 'default' : 'pointer' }}>
          <Icon name="upload" /> {v.kind === 'audio' ? 'Upload audio' : v.kind === 'video' ? 'Upload a video' : 'Upload an image'}
          <input type="file" accept={v.kind === 'audio' ? 'audio/*' : v.kind === 'video' ? 'video/*' : 'image/*'} hidden disabled={!!busy}
            onChange={async (e) => {
              const f = e.target.files?.[0]
              e.target.value = ''
              if (f) await run('img', async () => api.uploadVisualImage(jobId, v.id, f.name, await fileToDataUrl(f)))
            }} />
        </label>
        {v.kind !== 'video' && v.kind !== 'audio' && (
          <Button block size="sm" variant="ghost" icon="paste" disabled={!!busy}
            onClick={pasteImage}>Paste an image</Button>
        )}
        {v.kind !== 'audio' && (
        <div className="row gap-6">
          <input className="input" style={{ flex: 1, fontSize: 12.5 }} disabled={!!busy}
            placeholder={v.kind === 'video' ? 'Video URL or page…' : 'Image URL or page…'}
            value={urlDraft} onChange={(e) => setUrlDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && urlDraft.trim()) run('img', () => api.visualFromUrl(jobId, v.id, urlDraft.trim()).then(() => setUrlDraft(''))) }} />
          <Button size="sm" variant="ghost" icon="link" disabled={!!busy || !urlDraft.trim()}
            onClick={() => run('img', () => api.visualFromUrl(jobId, v.id, urlDraft.trim()).then(() => setUrlDraft('')))}>Fetch</Button>
        </div>
        )}
        {v.kind !== 'video' && v.kind !== 'audio' && (
          <GuidedRegenButton block size="sm" variant="ghost" icon="rotate-right"
            label={v.has_image ? 'Regenerate image' : 'Generate image'} busyLabel="Painting…"
            busy={busy === 'img'} disabled={!!busy}
            onRegen={(instr) => run('img', () => api.generateVisualImage(jobId, v.id, instr))} />
        )}
      </div>
    </Card>
  )
}

export default function ScriptVisuals({ jobId, sceneIds = [], castNames = [], settingHint = '',
                                        onAddCharacter, addingCharacter = false, children }) {
  const [visuals, setVisuals] = useState([])
  const [catalogue, setCatalogue] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = async () => {
    if (!jobId) return
    try {
      const r = await api.listVisuals(jobId)
      setVisuals(r.visuals || [])
      setCatalogue(r.catalogue || [])
    } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [jobId])

  const add = async (kind) => {
    setBusy(kind)
    try {
      // A location starts from the script's own setting text, so the first
      // "Generate image" already paints the right room.
      await api.addVisual(jobId, kind === 'location'
        ? { kind, name: 'Location', description: settingHint }
        : { kind, name: { wardrobe: 'Outfit', image: 'Reference', video: 'Video reference', audio: 'Soundtrack' }[kind] || 'Reference' })
      await load()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  return (
    <>
      <Banner tone="danger">{error}</Banner>
      <Card span={12} well>
        <div className="row between center row--wrap gap-10">
          <span style={{ fontSize: 13 }}>
            Everything the film renders from, in one place: the <strong>characters</strong>,
            the <strong>location</strong> that keeps every scene in the same room, the
            {' '}<strong>wardrobe</strong> that keeps a character in the same clothes — and
            any other <strong>image</strong> or <strong>video</strong> the model should match —
            plus <strong>soundtrack</strong> audio the takes are generated against.
          </span>
          <div className="row gap-8 row--wrap">
            {onAddCharacter && (
              <Button variant="primary" size="sm" icon="user-plus" disabled={addingCharacter}
                onClick={onAddCharacter}>{addingCharacter ? 'Adding…' : 'Add character'}</Button>
            )}
            <Button variant="ghost" size="sm" icon="location-dot" disabled={!!busy}
              onClick={() => add('location')}>Add location</Button>
            <Button variant="ghost" size="sm" icon="shirt" disabled={!!busy}
              onClick={() => add('wardrobe')}>Add wardrobe</Button>
            <Button variant="ghost" size="sm" icon="image" disabled={!!busy}
              onClick={() => add('image')}>Add image</Button>
            <Button variant="ghost" size="sm" icon="film" disabled={!!busy}
              onClick={() => add('video')}>Add video</Button>
            <Button variant="ghost" size="sm" icon="music" disabled={!!busy}
              onClick={() => add('audio')}>Add soundtrack</Button>
          </div>
        </div>
      </Card>
      {children}
      {visuals.map((v) => (
        v.readonly ? (
          /* The film's SONG: an input of every singing take, so it belongs on
             the wall — but it is edited in the Song tab, not here. */
          <Card key={v.id} span={4} className="stack gap-10">
            <span className="label-sm"><Icon name="music" /> {v.name}</span>
            <audio controls preload="none" src={v.audio_url} style={{ width: '100%' }} />
            <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>{v.description}</p>
          </Card>
        ) : (
        <VisualCard key={v.id} v={v} jobId={jobId} sceneIds={sceneIds} castNames={castNames}
          onChanged={load} onError={setError} />
        )
      ))}
      {/* Catalogue assets sit at the same level as the film's own — they feed
          the same <Picture N> slots — but edit in Settings, not here. */}
      {catalogue.map((a) => (
        <CatalogueRefCard key={`cat-${a.id}`} name={a.name}
          kind={(KIND_META[a.kind] || KIND_META.location).label}
          description={a.description} imageUrl={a.image_url}
          icon={(KIND_META[a.kind] || KIND_META.location).icon}
          editHint="Settings → Assets" />
      ))}
      {!visuals.length && !catalogue.length && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>
          No locations or wardrobe yet.</p></Card>
      )}
    </>
  )
}
