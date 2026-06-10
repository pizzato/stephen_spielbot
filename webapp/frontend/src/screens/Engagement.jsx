import { useState, useEffect, useRef } from 'react'
import { Card, Field, Button, Segmented, Icon, Banner, ProgressBar, Chip } from '../components.jsx'
import { api } from '../api.js'

function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(Math.round(n))
}
function fmtDate(ts) {
  if (!ts) return ''
  try { return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) }
  catch { return '' }
}

const PHASE_LABEL = {
  fetching: 'Fetching channel history & first-3-day views…',
  embedding: 'Computing embeddings… (first run downloads the model, ~130 MB)',
  training: 'Training & cross-validating…',
  done: 'Done.',
}
const PHASE_PCT = { fetching: 25, embedding: 55, training: 85, done: 100 }

const RELIABILITY = {
  ok:   { tone: 'ok',   text: 'The model beats the predict-the-average baseline on held-out videos.' },
  weak: { tone: 'warn', text: "The model barely beats guessing the channel average — treat predictions as a weak hint, not a forecast." },
  insufficient: { tone: 'warn', text: 'Trained on very few videos — predictions are a rough hint at best. Publish more, then rebuild.' },
}

// Actual-vs-predicted scatter on a log1p scale (views are heavy-tailed). Points
// near the dashed y=x line are accurate predictions. Dependency-free SVG.
function Scatter({ samples }) {
  const S = 300, pad = 30, plot = S - 2 * pad
  const vals = samples.flatMap((s) => [s.actual, s.predicted])
  const max = Math.max(1, ...vals)
  const lm = Math.log1p(max)
  const fx = (v) => pad + (Math.log1p(Math.max(0, v)) / lm) * plot
  const fy = (v) => (S - pad) - (Math.log1p(Math.max(0, v)) / lm) * plot
  return (
    <svg viewBox={`0 0 ${S} ${S}`} style={{ width: '100%', maxWidth: 360, display: 'block' }}>
      <rect x={pad} y={pad} width={plot} height={plot} fill="none" stroke="var(--border)" />
      <line x1={fx(0)} y1={fy(0)} x2={fx(max)} y2={fy(max)} stroke="var(--ink-3)" strokeDasharray="4 4" />
      {samples.map((s) => (
        <circle key={s.video_id} cx={fx(s.actual)} cy={fy(s.predicted)} r="4"
          fill="var(--accent)" opacity="0.6">
          <title>{`${s.title}\nactual ${s.actual} · predicted ${s.predicted}`}</title>
        </circle>
      ))}
      <text x={pad + plot / 2} y={S - 4} textAnchor="middle" fontSize="11" fill="var(--muted)">actual 3-day views →</text>
      <text x={12} y={pad + plot / 2} textAnchor="middle" fontSize="11" fill="var(--muted)"
        transform={`rotate(-90 12 ${pad + plot / 2})`}>predicted ↑</text>
    </svg>
  )
}

function Metric({ label, value, sub }) {
  return (
    <Card span={3}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, letterSpacing: '-0.02em' }}>{value}</div>
      {sub != null && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{sub}</div>}
    </Card>
  )
}

export default function Engagement() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [building, setBuilding] = useState(false)
  const [phase, setPhase] = useState('')
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  // One model per channel (issue #22) — the selector picks whose model is shown/built.
  const [channels, setChannels] = useState(null)
  const [channel, setChannel] = useState('')
  const pollRef = useRef(null)

  // try-an-idea predictor (live — estimates as you type)
  const [tTitle, setTTitle] = useState('')
  const [tDesc, setTDesc] = useState('')
  const [tShort, setTShort] = useState(false)
  const [pred, setPred] = useState(null)
  const [predicting, setPredicting] = useState(false)

  const load = (ch = channel) => {
    setLoading(true)
    return api.engagementStatus(ch).then(setStatus).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  useEffect(() => {
    api.ytChannels().then((r) => {
      const list = r.channels || []
      setChannels(list)
      setChannel((c) => c || list[0]?.id || '')
    }).catch(() => setChannels([]))
    return () => clearTimeout(pollRef.current)
  }, [])
  useEffect(() => { if (channels !== null) load() }, [channel, channels])

  const build = async () => {
    setBuilding(true); setError(''); setNote(''); setPhase('fetching')
    try {
      const { task_id } = await api.engagementBuild(channel)
      await new Promise((resolve, reject) => {
        const check = async () => {
          try {
            const s = await api.engagementBuildStatus(task_id)
            if (s.status === 'done') { resolve() }
            else if (s.status === 'error') { reject(new Error(s.error || 'Build failed')) }
            else { setPhase(s.phase || 'fetching'); pollRef.current = setTimeout(check, 2500) }
          } catch (e) { reject(e) }
        }
        check()
      })
      setNote('Model built.')
      await load()
    } catch (e) { setError(e.message) } finally { setBuilding(false); setPhase('') }
  }

  // Live estimate — debounced so it fires once typing settles (the model is fast).
  useEffect(() => {
    if (!tTitle.trim() && !tDesc.trim()) { setPred(null); setPredicting(false); return }
    setPredicting(true)
    const t = setTimeout(() => {
      api.engagementPredict({ title: tTitle, description: tDesc, is_short: tShort, channel })
        .then(setPred).catch(() => setPred(null)).finally(() => setPredicting(false))
    }, 400)
    return () => clearTimeout(t)
  }, [tTitle, tDesc, tShort, channel])

  const available = status?.available
  const content = status?.content
  const rel = status?.reliability
  const relInfo = RELIABILITY[rel]

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Predictive Model</span>
          <h1 className="display-md reveal reveal-d1">Predict a video's reach</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          {(channels || []).length > 1 && (
            <select className="select" value={channel} onChange={(e) => setChannel(e.target.value)} style={{ maxWidth: 220 }}>
              {(channels || []).map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
            </select>
          )}
          <Button variant={available ? 'ghost' : 'primary'} icon="wand-magic-sparkles"
            disabled={building} onClick={build}>
            {building ? 'Building…' : (available ? 'Rebuild model' : 'Build model')}
          </Button>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {note && <Banner tone="ok">{note}</Banner>}

      {building && (
        <Card span={12} className="reveal reveal-d1" style={{ marginBottom: 16 }}>
          <div className="row center gap-10" style={{ marginBottom: 10 }}>
            <Icon name="spinner" spin />
            <span style={{ fontSize: 13 }}>{PHASE_LABEL[phase] || 'Working…'}</span>
          </div>
          <ProgressBar pct={PHASE_PCT[phase] || 10} />
        </Card>
      )}

      {loading && !status && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>Loading…</p></Card>}

      {/* Empty / not-yet-built state */}
      {!loading && !available && !building && (
        <Card span={12} padLg className="reveal reveal-d1">
          <div className="row center gap-16">
            <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="chart-line" /></span>
            <div className="grow">
              <div style={{ fontWeight: 600, fontSize: 15 }}>No model yet</div>
              <p className="muted" style={{ fontSize: 13, margin: '6px 0 0', maxWidth: 640 }}>
                {status?.needs_rebuild
                  ? 'A model exists but is out of date (different library version or feature set). Rebuild it to use predictions again.'
                  : "Build a model from your channel's history to estimate how many views a new idea will get in its first 3 days. Predictions then appear on the Create screen and on YouTube ideas, with posting-time guidance on the Publish screen."}
              </p>
            </div>
            <Button variant="primary" icon="wand-magic-sparkles" disabled={building} onClick={build}>
              {status?.needs_rebuild ? 'Rebuild model' : 'Build model'}
            </Button>
          </div>
        </Card>
      )}

      {/* Built model — evaluation */}
      {!loading && available && content && (
        <div className="bento">
          {relInfo && (
            <div className="col-12">
              <Banner tone={relInfo.tone}>{relInfo.text}</Banner>
            </div>
          )}

          <Metric label="Typical error" value={`±${fmtNum(content.mae)}`}
            sub={`views · guessing the average: ±${fmtNum(content.baseline_mae)}`} />
          <Metric label="Correlation" value={content.pearson != null ? content.pearson.toFixed(2) : '—'}
            sub="actual vs predicted (1 = perfect)" />
          <Metric label="Beats guessing?" value={rel === 'ok' ? 'Yes' : 'Not yet'}
            sub={`reliability: ${rel}`} />
          <Metric label="Trained on" value={fmtNum(status.n_samples)}
            sub={`videos · ${fmtNum(status.n_dropped)} excluded`} />

          <Card span={6} className="reveal reveal-d1">
            <div style={{ fontWeight: 600, marginBottom: 6 }}>Held-out accuracy</div>
            <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
              Each point is one video predicted by a model that never saw it (cross-validation).
            </p>
            {status.samples?.length ? <Scatter samples={status.samples} />
              : <p className="muted" style={{ fontSize: 12 }}>Not enough videos to plot.</p>}
          </Card>

          <Card span={6} className="reveal reveal-d2">
            <div className="row center between">
              <div style={{ fontWeight: 600 }}>Try an idea</div>
              {predicting && <span className="muted" style={{ fontSize: 11 }}><Icon name="spinner" spin /> estimating…</span>}
            </div>
            <div className="stack gap-10" style={{ marginTop: 10 }}>
              <Field label="Title">
                <input className="input" placeholder="The rise and fall of the Roman Empire"
                  value={tTitle} onChange={(e) => setTTitle(e.target.value)} />
              </Field>
              <Field label="Description" hint="Optional — the angle or focus.">
                <textarea className="textarea" rows={2} value={tDesc} onChange={(e) => setTDesc(e.target.value)} />
              </Field>
              <Field label="Format" hint="Shorts and long-form get very different reach.">
                <Segmented value={tShort ? 'short' : 'long'} onChange={(v) => setTShort(v === 'short')}
                  options={[{ value: 'long', label: 'Long-form' }, { value: 'short', label: 'Short' }]} />
              </Field>
              {pred && pred.available && (
                <div className="row center gap-10" style={{ padding: '12px 14px', background: 'var(--accent-soft)', borderRadius: 'var(--r-md)' }}>
                  <div style={{ fontSize: 28, fontWeight: 700, color: 'var(--accent)' }}>{fmtNum(pred.predicted_views)}</div>
                  <div className="muted" style={{ fontSize: 12 }}>predicted views in the first 3 days<br />reliability: {pred.reliability}</div>
                </div>
              )}
              {pred && !pred.available && <span className="muted" style={{ fontSize: 12 }}>Could not estimate — try rebuilding the model.</span>}
            </div>
          </Card>

          <Card span={12} well className="reveal reveal-d3">
            <div className="row center between row--wrap gap-10" style={{ fontSize: 12.5 }}>
              <span className="muted">
                Built {fmtDate(status.built_at)} · embeddings: {status.embed_model} · {status.n_short ?? 0} of {status.n_samples} Shorts ·
                videos newer than {status.data_lag_days} days excluded (no full 3-day window yet)
              </span>
              <span className="muted">
                Timing model correlation: {status.timing?.pearson != null ? status.timing.pearson.toFixed(2) : '—'} · drives the Publish tab's best-time guidance
              </span>
            </div>
          </Card>
        </div>
      )}
    </div>
  )
}
