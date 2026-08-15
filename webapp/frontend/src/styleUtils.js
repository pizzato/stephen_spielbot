// Style hierarchy resolution (mirrors the backend's _style_lineage /
// style_settings): a style with a `parent` stores only its overridden fields;
// everything else resolves through the parent chain, nearest ancestor winning.
// Root styles come back from the server dense, so a resolved style always has
// every field — the UI can read it exactly like a pre-hierarchy style object.

// Free-text fields where an override may embed the literal marker "{parent}":
// it is replaced with the parent's RESOLVED text for that field (empty at the
// chain root), so a child can extend the parent's instructions — before,
// after, or around its own — instead of replacing them. Composition recurses
// through grandparents. Mirrors STYLE_TEXT_FIELDS in app.py — keep in sync.
export const STYLE_TEXT_FIELDS = new Set([
  'description', 'visual_style', 'video_style', 'video_negative_prompt',
  'title_style', 'extra_instructions', 'script_avoid', 'description_suffix',
  'attribution_description', 'attribution_hashtags', 'attribution_youtube_tags',
])

export const PARENT_MARKER = '{parent}'

// Automation flags a style may override → the flat config key holding the
// GLOBAL value. Mirrors AUTOMATION_FIELD_TO_FLAT in app.py — keep in sync.
// A style stores only what it changes, in a sparse `automation` object, so an
// absent flag keeps following its parent chain and ultimately the global.
export const AUTOMATION_FIELDS = {
  auto_start_job: 'youtube_auto_start_job',
  auto_write_scripts: 'youtube_auto_write_scripts',
  auto_approve_script: 'youtube_auto_approve_script',
  auto_critic: 'youtube_auto_critic',
  auto_critic_passes: 'youtube_auto_critic_passes',
  auto_format: 'youtube_auto_format',
  auto_song: 'youtube_auto_song',
  auto_song_critic_passes: 'youtube_auto_song_critic_passes',
  auto_song_voice: 'youtube_auto_song_voice',
  auto_song_revoice: 'youtube_auto_song_revoice',
  auto_song_approve: 'youtube_auto_song_approve',
}

// The global baseline: the flat keys, as every style sees them before any
// override. `cfg` is the whole config object.
export function globalAutomation(cfg) {
  const out = {}
  for (const [k, flat] of Object.entries(AUTOMATION_FIELDS)) out[k] = cfg?.[flat]
  return out
}

// Automation flags as they apply to one style (mirrors app.automation_settings):
// the global baseline with each ancestor's overrides laid over it, nearest
// winning. `upto` trims the chain — pass -1 to get what the style INHERITS,
// i.e. what it would fall back to if it dropped its own overrides.
export function resolveAutomation(styles, name, cfg, upto = 0) {
  const chain = styleLineage(styles, name)
  const out = globalAutomation(cfg)
  for (const s of (upto ? chain.slice(0, upto) : chain)) Object.assign(out, s.automation || {})
  return out
}

// Which style an inherited flag comes from — '' meaning the global baseline.
// Ignores the style's own overrides, so it always names the fallback source.
export function automationSource(styles, name, field) {
  const chain = styleLineage(styles, name).slice(0, -1)
  for (let i = chain.length - 1; i >= 0; i--) {
    if (chain[i].automation && field in chain[i].automation) return chain[i].name
  }
  return ''
}

// Ancestry chain [root, …, name] of RAW style objects (mirrors the backend's
// _style_lineage). Empty when no such style exists. Safe on dangling parents
// (the walk just stops) and cycles (never revisits a style).
export function styleLineage(styles, name) {
  const list = (styles || []).filter((s) => s && s.name)
  const byName = new Map(list.map((s) => [s.name, s]))
  const chain = []
  const seen = new Set()
  let cur = byName.get(name)
  while (cur && !seen.has(cur.name)) {
    seen.add(cur.name)
    chain.unshift(cur)
    cur = cur.parent ? byName.get(cur.parent) : null
  }
  return chain
}

// Depth-first display order of the hierarchy: each root followed by its
// descendants, config order among siblings — so pickers can show the tree.
// Returns [{style, depth}]. A style whose parent is missing lists as a root;
// members of a (hand-edited) cycle are appended rather than dropped.
export function styleTreeOrder(styles) {
  const list = (styles || []).filter((s) => s && s.name)
  const byName = new Map(list.map((s) => [s.name, s]))
  const kids = new Map()
  const roots = []
  for (const s of list) {
    const p = s.parent && s.parent !== s.name ? byName.get(s.parent) : null
    if (p) {
      if (!kids.has(p.name)) kids.set(p.name, [])
      kids.get(p.name).push(s)
    } else {
      roots.push(s)
    }
  }
  const out = []
  const seen = new Set()
  const walk = (s, depth) => {
    if (seen.has(s.name)) return
    seen.add(s.name)
    out.push({ style: s, depth })
    for (const c of kids.get(s.name) || []) walk(c, depth + 1)
  }
  roots.forEach((r) => walk(r, 0))
  list.forEach((s) => walk(s, 0))
  return out
}

// Effective settings for `name`, or null when no such style exists (matching
// list.find() semantics so callers keep their existing fallbacks).
export function resolveStyle(styles, name) {
  const chain = styleLineage(styles, name)
  if (!chain.length) return null
  const target = chain[chain.length - 1]
  const out = {}
  chain.forEach((s, i) => {
    for (const k of Object.keys(s)) {
      if (k === 'name' || k === 'parent') continue
      let v = s[k]
      if (k === 'automation') {
        // Sparse per-FLAG overrides, not a whole-object one: a child that sets
        // a single flag must not wipe out its parent's other flags.
        out.automation = { ...(out.automation || {}), ...(v || {}) }
        continue
      }
      if (STYLE_TEXT_FIELDS.has(k) && typeof v === 'string' && v.includes(PARENT_MARKER)) {
        const pv = String((i ? out[k] : '') ?? '')
        v = v.split(PARENT_MARKER).join(pv).trim()
      }
      out[k] = v
    }
  })
  out.name = target.name
  if (target.parent) out.parent = target.parent
  return out
}
