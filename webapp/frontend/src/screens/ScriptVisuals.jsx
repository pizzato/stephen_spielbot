import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Card, Field, Button, Icon, Banner, GuidedRegenButton, CatalogueRefCard } from '../components.jsx'

const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = reject
  r.readAsDataURL(file)
})

// Locations and wardrobe: the reference images that stop a performance film
// drifting between scenes. Words alone don't hold a room or an outfit still —
// these ride the same <Picture N> slots as the cast, after it.

function VisualCard({ v, jobId, sceneIds, castNames, onChanged, onError }) {
  const [busy, setBusy] = useState('')
  const run = async (what, fn) => {
    setBusy(what)
    try { await fn(); await onChanged() } catch (e) { onError(e.message) } finally { setBusy('') }
  }
  const patch = (body) => run('save', () => api.updateVisual(jobId, v.id, body))
  const everyScene = !v.scenes || v.scenes.length === 0

  return (
    <Card span={4} className="stack gap-12">
      <div className="row between center">
        <span className="label-sm">{v.kind === 'wardrobe' ? 'Wardrobe' : 'Location'}</span>
        <Button variant="quiet" size="sm" icon="trash-can" disabled={!!busy}
          onClick={() => run('del', () => api.deleteVisual(jobId, v.id))} />
      </div>

      {v.image_url
        ? <img src={v.image_url} alt={v.name}
            style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', borderRadius: 10 }} />
        : <div style={{ width: '100%', aspectRatio: '1', borderRadius: 10, display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'var(--well, rgba(127,127,127,.10))',
                        border: '1px dashed var(--line, #ccc)' }}>
            <Icon name={v.kind === 'wardrobe' ? 'shirt' : 'location-dot'}
              style={{ color: 'var(--ink-3)', fontSize: 26 }} />
          </div>}

      <Field label="Name">
        <input className="input" defaultValue={v.name} disabled={!!busy}
          onBlur={(e) => e.target.value !== v.name && patch({ name: e.target.value })} />
      </Field>
      <Field label="Description" hint="What the image should show. Painted with no people in it.">
        <textarea className="textarea" rows={2} defaultValue={v.description} disabled={!!busy}
          onBlur={(e) => e.target.value !== v.description && patch({ description: e.target.value })} />
      </Field>

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
          <Icon name="upload" /> Upload an image
          <input type="file" accept="image/*" hidden disabled={!!busy}
            onChange={async (e) => {
              const f = e.target.files?.[0]
              e.target.value = ''
              if (f) await run('img', async () => api.uploadVisualImage(jobId, v.id, f.name, await fileToDataUrl(f)))
            }} />
        </label>
        <GuidedRegenButton block size="sm" variant="ghost" icon="rotate-right"
          label={v.has_image ? 'Regenerate image' : 'Generate image'} busyLabel="Painting…"
          busy={busy === 'img'} disabled={!!busy}
          onRegen={(instr) => run('img', () => api.generateVisualImage(jobId, v.id, instr))} />
      </div>
    </Card>
  )
}

export default function ScriptVisuals({ jobId, sceneIds = [], castNames = [], settingHint = '' }) {
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
        : { kind, name: 'Outfit' })
      await load()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  return (
    <>
      <Banner tone="danger">{error}</Banner>
      <Card span={12} well>
        <div className="row between center row--wrap gap-10">
          <span style={{ fontSize: 13 }}>
            Reference images that hold a performance film together: a <strong>location</strong>
            {' '}keeps every scene in the same room, <strong>wardrobe</strong> keeps a character
            {' '}in the same clothes. Without them the model re-imagines both each scene.
          </span>
          <div className="row gap-8">
            <Button variant="primary" size="sm" icon="location-dot" disabled={!!busy}
              onClick={() => add('location')}>Add location</Button>
            <Button variant="ghost" size="sm" icon="shirt" disabled={!!busy}
              onClick={() => add('wardrobe')}>Add wardrobe</Button>
          </div>
        </div>
      </Card>
      {visuals.map((v) => (
        <VisualCard key={v.id} v={v} jobId={jobId} sceneIds={sceneIds} castNames={castNames}
          onChanged={load} onError={setError} />
      ))}
      {/* Catalogue assets sit at the same level as the film's own — they feed
          the same <Picture N> slots — but edit in Settings, not here. */}
      {catalogue.map((a) => (
        <CatalogueRefCard key={`cat-${a.id}`} name={a.name}
          kind={a.kind === 'wardrobe' ? 'Wardrobe' : 'Location'}
          description={a.description} imageUrl={a.image_url}
          icon={a.kind === 'wardrobe' ? 'shirt' : 'location-dot'}
          editHint="Settings → Assets" />
      ))}
      {!visuals.length && !catalogue.length && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>
          No locations or wardrobe yet.</p></Card>
      )}
    </>
  )
}
