// Reusable primitives for the redesign — ported from the design system's
// components.jsx / App.jsx into ES modules.
import React from 'react'

export function Icon({ name, brand, style, spin }) {
  return <i className={`${brand ? 'fa-brands' : 'fa-solid'} fa-${name}${spin ? ' fa-spin' : ''}`} style={style}></i>
}

export function Button({ variant = 'ghost', size, block, icon, iconRight, children, onClick, disabled, type = 'button' }) {
  const cls = ['btn', `btn--${variant}`, size === 'lg' ? 'btn--lg' : '', block ? 'btn--block' : '']
    .filter(Boolean).join(' ')
  return (
    <button type={type} className={cls} onClick={onClick} disabled={disabled}>
      {icon ? <Icon name={icon} /> : null}
      {children}
      {iconRight ? <Icon name={iconRight} /> : null}
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

export function Check({ checked, onChange, label }) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} onChange={(e) => onChange?.(e.target.checked)} />
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

export function PageHead({ kicker, title, children }) {
  return (
    <div className="page-head">
      <div className="page-head__intro">
        <span className="label-sm reveal">{kicker}</span>
        <h1 className="display-md reveal reveal-d1">{title}</h1>
      </div>
      {children ? <div className="row gap-10 reveal reveal-d1 center">{children}</div> : null}
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
  { id: 'youtube', label: 'YouTube', icon: 'youtube', brand: true },
  { id: 'library', label: 'Films', icon: 'film' },
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
  const counts = { queue: badges.queue, youtube: badges.youtube, library: badges.films }
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
