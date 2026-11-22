import { Button, Chip } from '../components.jsx'

export default function SunoTakes({ takes, busy, importing, onUse }) {
  if (!takes?.length) return null
  return (
    <div className="stack gap-12" style={{ borderTop: '1px solid var(--line)', paddingTop: 16, marginTop: 16 }}>
      <span className="label-sm">Suno previews</span>
      <div className="field__hint">Play on Suno to listen before downloading. Use this take imports only your chosen song and may spend one download credit. The imported song joins your saved versions.</div>
      {takes.map((take, i) => (
        <div key={take.id} className="stack gap-8" style={{ border: '1px solid var(--line)', borderRadius: 8, padding: 12 }}>
          <div className="row center gap-8 row--wrap">
            <strong>{take.title || 'Suno song'}</strong>
            <span className="muted" style={{ fontSize: 12 }}>Take {i + 1}</span>
            {take.model && <span className="muted" style={{ fontSize: 12 }}>{take.model}</span>}
            {take.status === 'pending' && <Chip tone="warn">Still generating</Chip>}
            {take.imported && <Chip tone="ok">imported</Chip>}
          </div>
          <div className="row center gap-12 row--wrap">
            {take.song_url && <a href={take.song_url} target="_blank" rel="noopener noreferrer">Play on Suno</a>}
            <Button variant="primary" icon="check" disabled={busy} onClick={() => onUse(take.id)}>
              {importing === take.id ? take.status === 'pending' ? 'Waiting and importing…' : 'Importing…' : take.imported ? 'Use imported take' : 'Use this take'}
            </Button>
          </div>
        </div>
      ))}
    </div>
  )
}
