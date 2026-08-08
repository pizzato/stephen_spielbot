import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Card, Field, Button, Icon, Banner, GuidedRegenButton } from '../components.jsx'

// The reusable half of the visual library: locations and wardrobe defined once
// and cast by any film, scoped exactly like characters (no style = the global
// pool; a style = that style and its children). Characters keep their own tab —
// they carry voices, aliases and casting rules these do not.

const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = reject
  r.readAsDataURL(file)
})

function AssetCard({ a, styles, onPatch, onRemove, onImage, onUpload }) {
  const [busy, setBusy] = useState('')
  const run = async (what, fn) => {
    setBusy(what)
    try { await fn() } finally { setBusy('') }
  }
  return (
    <Card span={4} className="stack gap-12">
      <div className="row between center">
        <span className="label-sm">{a.kind === 'wardrobe' ? 'Wardrobe' : 'Location'}</span>
        <Button variant="quiet" size="sm" icon="trash-can" disabled={!!busy} onClick={onRemove} />
      </div>

      {a.image_url
        ? <img src={a.image_url} alt={a.name}
            style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', borderRadius: 10 }} />
        : <div style={{ width: '100%', aspectRatio: '1', borderRadius: 10, display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'var(--well, rgba(127,127,127,.10))',
                        border: '1px dashed var(--line, #ccc)' }}>
            <Icon name={a.kind === 'wardrobe' ? 'shirt' : 'location-dot'}
              style={{ color: 'var(--ink-3)', fontSize: 26 }} />
          </div>}

      <Field label="Name">
        <input className="input" defaultValue={a.name} disabled={!!busy}
          onBlur={(e) => e.target.value !== a.name && onPatch({ name: e.target.value })} />
      </Field>
      <Field label="Description" hint="What the image shows. Painted with no people in it.">
        <textarea className="textarea" rows={2} defaultValue={a.description} disabled={!!busy}
          onBlur={(e) => e.target.value !== a.description && onPatch({ description: e.target.value })} />
      </Field>
      <Field label="Kind">
        <select className="select" value={a.kind} disabled={!!busy}
          onChange={(e) => onPatch({ kind: e.target.value })}>
          <option value="location">Location — where a scene happens</option>
          <option value="wardrobe">Wardrobe — what someone wears</option>
        </select>
      </Field>
      <Field label="Available to" hint="Global assets are inherited by every style.">
        <select className="select" value={a.style || ''} disabled={!!busy}
          onChange={(e) => onPatch({ style: e.target.value })}>
          <option value="">Global — every style</option>
          {styles.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
        </select>
      </Field>

      <div className="stack gap-6">
        <label className="btn btn--ghost btn--sm btn--block" style={{ cursor: busy ? 'default' : 'pointer' }}>
          <Icon name="upload" /> Upload an image
          <input type="file" accept="image/*" hidden disabled={!!busy}
            onChange={async (e) => {
              const f = e.target.files?.[0]
              e.target.value = ''
              if (f) await run('img', async () => onUpload(f.name, await fileToDataUrl(f)))
            }} />
        </label>
        <GuidedRegenButton block size="sm" variant="ghost" icon="rotate-right"
          label={a.has_image ? 'Regenerate image' : 'Generate image'} busyLabel="Painting…"
          busy={busy === 'gen'} disabled={!!busy}
          onRegen={(instr) => run('gen', () => onImage(instr))} />
      </div>
    </Card>
  )
}

export default function SettingsAssets({ styles = [] }) {
  const [assets, setAssets] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try { setAssets((await api.listAssets()).assets || []) } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])

  const persist = async (next) => {
    setBusy(true)
    try { setAssets((await api.saveAssets(next)).assets || []) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  const patch = (id, body) => persist(assets.map((a) => (a.id === id ? { ...a, ...body } : a)))
  const remove = (id) => persist(assets.filter((a) => a.id !== id))
  const add = (kind) => persist([...assets, {
    name: kind === 'wardrobe' ? 'New outfit' : 'New location', kind, description: '', style: '',
  }])
  const genImage = async (id, instr) => {
    const a = assets.find((x) => x.id === id)
    setAssets((await api.generateAssetImage(id, a?.style || '', instr)).assets || [])
  }
  const upload = async (id, filename, data) => {
    setAssets((await api.uploadAssetImage(id, filename, data)).assets || [])
  }

  return (
    <>
      <Banner tone="danger">{error}</Banner>
      <Card span={12} well>
        <div className="row between center row--wrap gap-10">
          <span style={{ fontSize: 13 }}>
            Locations and wardrobe any film can cast — define the studio once and every
            episode shoots in the same room. Characters live in their own tab.
          </span>
          <div className="row gap-8">
            <Button variant="primary" size="sm" icon="location-dot" disabled={busy}
              onClick={() => add('location')}>Add location</Button>
            <Button variant="ghost" size="sm" icon="shirt" disabled={busy}
              onClick={() => add('wardrobe')}>Add wardrobe</Button>
          </div>
        </div>
      </Card>
      {assets.map((a) => (
        <AssetCard key={a.id} a={a} styles={styles}
          onPatch={(body) => patch(a.id, body)}
          onRemove={() => remove(a.id)}
          onImage={(instr) => genImage(a.id, instr)}
          onUpload={(fn, data) => upload(a.id, fn, data)} />
      ))}
      {!assets.length && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>
          No assets yet — add a location or an outfit.</p></Card>
      )}
    </>
  )
}
