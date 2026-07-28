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

// Effective settings for `name`, or null when no such style exists (matching
// list.find() semantics so callers keep their existing fallbacks). Safe on
// dangling parents (the walk just stops) and cycles (never revisits a style).
export function resolveStyle(styles, name) {
  const list = (styles || []).filter((s) => s && s.name)
  const byName = new Map(list.map((s) => [s.name, s]))
  const target = byName.get(name)
  if (!target) return null
  const chain = []
  const seen = new Set()
  let cur = target
  while (cur && !seen.has(cur.name)) {
    seen.add(cur.name)
    chain.unshift(cur)
    cur = cur.parent ? byName.get(cur.parent) : null
  }
  const out = {}
  chain.forEach((s, i) => {
    for (const k of Object.keys(s)) {
      if (k === 'name' || k === 'parent') continue
      let v = s[k]
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
