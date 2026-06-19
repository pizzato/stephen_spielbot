// Reusable primitives for the redesign — ported from the design system's
// components.jsx / App.jsx into ES modules.

export function Icon({ name, brand, style, spin }) {
  return <i className={`${brand ? 'fa-brands' : 'fa-solid'} fa-${name}${spin ? ' fa-spin' : ''}`} style={style}></i>
}

export function Button({ variant = 'ghost', size, block, icon, iconRight, brand, children, onClick, disabled, type = 'button' }) {
  const cls = ['btn', `btn--${variant}`, size === 'lg' ? 'btn--lg' : '', block ? 'btn--block' : '']
    .filter(Boolean).join(' ')
  return (
    <button type={type} className={cls} onClick={onClick} disabled={disabled}>
      {icon ? <Icon name={icon} brand={brand} /> : null}
      {children}
      {iconRight ? <Icon name={iconRight} brand={brand} /> : null}
    </button>
  )
}

export function Chip({ tone = 'neutral', dot, children }) {
  return <span className={`chip chip--${tone}`}>{dot ? <span className="chip__dot"></span> : null}{children}</span>
}

export function Card({ span, rowSpan, well, padLg, link, onClick, href, className = '', children, style }) {
  const cls = ['card', link ? 'card-link' : '', well ? 'card--well' : '', padLg ? 'card--pad-lg' : '',
    span ? `col-${span}` : '', rowSpan ? 'row-2' : '', className].filter(Boolean).join(' ')
  if (href) return <a className={cls} href={href} target="_blank" rel="noopener" style={style}>{children}</a>
  return <div className={cls} onClick={onClick} style={style}>{children}</div>
}

// A field label with an inline "Re-generate" button on the right (issue #88).
// `busy` disables the button and swaps in `busyLabel`; `onRegen` runs the LLM
// regeneration. Pass it as the `label` of a <Field>.
export function RegenLabel({ children, busy, disabled, onRegen, icon, label = 'Re-generate', busyLabel = 'Writing…' }) {
  return (
    <span className="row center between">
      <span className="row center gap-10">{icon ? <Icon name={icon} style={{ color: 'var(--ink-3)', width: 16 }} /> : null}{children}</span>
      <button type="button" className="btn btn--quiet" style={{ padding: '3px 9px', fontSize: 11 }}
        disabled={busy || disabled} onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRegen() }}>
        <Icon name="rotate" /> {busy ? busyLabel : label}
      </button>
    </span>
  )
}

export function Field({ label, hint, children }) {
  return (
    <div className="field">
      {label ? <label className="field__label">{label}</label> : null}
      {children}
      {hint ? <div className="field__hint">{hint}</div> : null}
    </div>
  )
}

export function Segmented({ options, value, onChange }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.value ?? o} className={`seg__opt ${(o.value ?? o) === value ? 'is-active' : ''}`}
          onClick={() => onChange(o.value ?? o)}>{o.label ?? o}</button>
      ))}
    </div>
  )
}

// Compose the backend resolution name string for an (orientation, pixel tier)
// pair, picked from meta.resolutions so the result is always a name the backend
// already understands. Names look like "<Orientation>[ <Label>] (<w>×<h>)"; the
// base tier has no label. Returns "" if no matching name exists.
export function composeResolution(meta = {}, orientation, tierKey) {
  const tier = (meta.pixel_tiers || []).find((t) => t.key === tierKey)
  const label = tier ? tier.label : ''
  const prefix = label ? `${orientation} ${label} (` : `${orientation} (`
  return (meta.resolutions || []).find((n) => n.startsWith(prefix)) || ''
}

// Return the pixel-tier key (e.g. "fhd") of a resolution name string, or "" if
// it is not one of the known names. The base tier (no label) is matched last so
// a labelled name like "...HD (...)" is not mistaken for the base tier.
export function resolutionTier(meta = {}, name) {
  if (!name) return ''
  const tiers = meta.pixel_tiers || []
  const labelled = tiers.filter((t) => t.label)
  const base = tiers.filter((t) => !t.label)
  for (const t of [...labelled, ...base]) {
    const tag = t.label ? ` ${t.label} (` : ' ('
    if (name.includes(tag)) return t.key
  }
  return ''
}

// Resolution picker: an orientation toggle (Landscape/Portrait/Square) plus a
// pixels/quality toggle. Emits the same composed name string the backend uses
// (e.g. "Landscape FHD (1920×1080)").
export function ResolutionPicker({ value, onChange, meta = {} }) {
  const orientations = meta.orientations || []
  const tiers = meta.pixel_tiers || []

  // Parse the current value back into orientation + tier so the toggles reflect it.
  const current = (() => {
    for (const o of orientations) {
      for (const t of tiers) {
        if (value && composeResolution(meta, o, t.key) === value) return { orientation: o, tier: t.key }
      }
    }
    return { orientation: meta.default_orientation || '', tier: meta.default_pixels || '' }
  })()

  const emit = (orientation, tierKey) => {
    const name = composeResolution(meta, orientation, tierKey)
    if (name) onChange(name)
  }

  return (
    <div className="row gap-10 row--wrap">
      <Segmented
        options={orientations.map((o) => ({ value: o, label: o }))}
        value={current.orientation}
        onChange={(o) => emit(o, current.tier)}
      />
      <Segmented
        options={tiers.map((t) => ({ value: t.key, label: t.label || 'Standard' }))}
        value={current.tier}
        onChange={(k) => emit(current.orientation, k)}
      />
    </div>
  )
}

export function Check({ checked, onChange, label, disabled }) {
  return (
    <label className="check" style={disabled ? { opacity: 0.55, cursor: 'default' } : undefined}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange?.(e.target.checked)} />
      <span>{label}</span>
    </label>
  )
}

export function ProgressBar({ pct }) {
  return <div className="pbar"><div className="pbar__fill" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}></div></div>
}

// A scene thumbnail: a real preview image if we have one, else a colored
// gradient placeholder (or a shimmer skeleton while rendering).
export function Thumb({ variant = 0, loading, label, src, aspect }) {
  return (
    <div className="scene__img" style={aspect ? { aspectRatio: aspect, background: 'var(--paper-2)' } : undefined}>
      {src
        ? <img src={src} alt="" style={{ width: '100%', height: '100%', objectFit: aspect ? 'contain' : 'cover', position: 'absolute', inset: 0 }} />
        : <div className={`gfill ${loading ? 'skel' : 'g' + (variant % 6)}`}></div>}
      {label ? <span className="scene__no">{label}</span> : null}
      {loading ? <div className="scene__state">rendering…</div> : null}
    </div>
  )
}

export function Banner({ tone = 'danger', children }) {
  if (!children) return null
  const bg = { danger: 'var(--danger-soft)', ok: 'var(--ok-soft)', info: 'var(--info-soft)', warn: 'var(--warn-soft)' }[tone]
  const fg = { danger: 'var(--danger)', ok: 'var(--ok)', info: 'var(--info)', warn: 'var(--warn)' }[tone]
  return (
    <div className="row center gap-10" style={{ padding: '10px 14px', background: bg, color: fg, borderRadius: 'var(--r-md)', fontSize: 13, marginBottom: 16 }}>
      <Icon name="circle-info" /> <span>{children}</span>
    </div>
  )
}

const NAV = [
  { id: 'home', label: 'Home', icon: 'house' },
  { id: 'create', label: 'Create', icon: 'wand-magic-sparkles' },
  { id: 'script', label: 'Script', icon: 'feather-pointed' },
  { id: 'progress', label: 'Render', icon: 'gauge-high' },
  { sep: true },
  { id: 'queue', label: 'Queue', icon: 'layer-group' },
  { id: 'community', label: 'Community', icon: 'comments' },
  { id: 'publish', label: 'Publishing', icon: 'upload' },
  { id: 'analytics', label: 'Analytics', icon: 'chart-simple' },
  { id: 'ideas', label: 'AI ideas', icon: 'lightbulb' },
  { id: 'library', label: 'Films', icon: 'film' },
  { id: 'engagement', label: 'Predictive Model', icon: 'chart-line' },
  { sep: true },
  { id: 'settings', label: 'Settings', icon: 'gear' },
]

// Mailbox-style sidebar indicator for a nav item: a live "REC" pill while a
// render is running, otherwise a count of items needing attention.
function navIndicator(id, badges) {
  if (id === 'progress' && badges.render_active) {
    return (
      <span className="nav__rec" title="Rendering now">
        <span className="nav__rec-dot"></span>
        {badges.render_pct ? `${badges.render_pct}%` : 'REC'}
      </span>
    )
  }
  const counts = { queue: badges.queue, community: badges.youtube, publish: badges.youtube_publishable, library: badges.films }
  const n = counts[id]
  return n ? <span className="nav__badge nav__badge--attn">{n}</span> : null
}

export function Sidebar({ route, go, badges = {} }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <img className="brand__mark" src="/assets/StephenSpielbot.png" alt="" />
        <div>
          <div className="brand__name">Spielbot</div>
          <div className="brand__sub">AI film studio</div>
        </div>
      </div>
      <nav className="nav">
        {NAV.map((n, i) => (
          n.sep
            ? <div key={'sep' + i} className="nav__sep"></div>
            : <button key={n.id} className={`nav__item ${route === n.id ? 'is-active' : ''}`} onClick={() => go(n.id)}>
                <Icon name={n.icon} brand={n.brand} />
                <span>{n.label}</span>
                {navIndicator(n.id, badges)}
              </button>
        ))}
      </nav>
      <div className="sidebar__foot">
        <div className="channel">
          <span className="channel__avatar"><Icon name="youtube" brand /></span>
          <div className="grow">
            <div className="channel__name">@StephenSpielbot</div>
            <div className="channel__meta">AI slop video director</div>
          </div>
        </div>
      </div>
    </aside>
  )
}
