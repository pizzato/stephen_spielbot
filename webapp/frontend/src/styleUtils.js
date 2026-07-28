// Style hierarchy resolution (mirrors the backend's _style_lineage /
// style_settings): a style with a `parent` stores only its overridden fields;
// everything else resolves through the parent chain, nearest ancestor winning.
// Root styles come back from the server dense, so a resolved style always has
// every field — the UI can read it exactly like a pre-hierarchy style object.

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
  for (const s of chain) {
    for (const k of Object.keys(s)) {
      if (k !== 'name' && k !== 'parent') out[k] = s[k]
    }
  }
  out.name = target.name
  if (target.parent) out.parent = target.parent
  return out
}
