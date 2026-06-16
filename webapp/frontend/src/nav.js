// nav.js — the single source of truth mapping every page to a URL and back.
//
// Routing is hash-based (#/...) so deep links, refresh, and back/forward all
// work against the SPA's single index.html with no server-side routing.
//
// Video-scoped pages carry the work_dir BASENAME — the readable
// "<slug>-<timestamp>" id — so a link reads like "#/edit/abc-clip-20260607",
// i.e. "the edit of the ABC video". To add a page later, add one ROUTES entry;
// the rest of the app keeps working unchanged.

// route id ⇄ URL shape.
//   seg:  leading path segment(s); [] is the root (#/).
//   name: true → an optional trailing work_dir-basename segment.
//   sub:  a fixed segment inserted before the name (e.g. youtube/publish/<name>).
const ROUTES = {
  home:     { seg: [] },
  create:   { seg: ['create'] },
  script:   { seg: ['script'],   name: true },
  progress: { seg: ['render'],   name: true },
  remix:    { seg: ['remix'],    name: true },
  editfilm: { seg: ['edit'],     name: true },
  queue:    { seg: ['queue'] },
  analytics: { seg: ['analytics'] },
  community: { seg: ['community'] },
  publish:  { seg: ['publish'],  name: true },
  ideas:    { seg: ['ideas'] },
  library:  { seg: ['films'] },
  engagement: { seg: ['engagement'] },
  settings: { seg: ['settings'] },
}

// First URL segment → route id, for parsing.
const BY_SEG = Object.fromEntries(
  Object.entries(ROUTES).filter(([, r]) => r.seg.length).map(([id, r]) => [r.seg[0], id]),
)

// Build the hash for a navigation, e.g. buildHash('editfilm', {name}) → '#/edit/abc'.
// Unknown routes collapse to the root.
export function buildHash(route, { name } = {}) {
  const r = ROUTES[route] || ROUTES.home
  const segs = [...r.seg]
  if (r.name && name) {
    if (r.sub) segs.push(r.sub)
    segs.push(name)
  }
  return '#/' + segs.join('/')
}

// Parse location.hash → { route, name?, view? }. Anything unrecognized → home,
// so a stale or hand-edited URL degrades gracefully instead of breaking.
export function parseHash(hash) {
  const segs = (hash || '').replace(/^#/, '').split('/').filter(Boolean)
  if (!segs.length) return { route: 'home' }
  const route = BY_SEG[segs[0]]
  if (!route) return { route: 'home' }
  const r = ROUTES[route]
  if (r.sub && segs[1] === r.sub) return { route, view: r.sub, name: segs[2] }
  if (r.name) return { route, name: segs[1] }
  return { route }
}

// work_dir ⇄ basename. Every work_dir is a direct child of the videos dir, so
// the basename identifies it uniquely and round-trips losslessly.
export const nameOf = (workDir) => (workDir || '').replace(/\/+$/, '').split('/').pop()
export const pathOf = (name, videosDir) =>
  (name && videosDir ? `${videosDir.replace(/\/+$/, '')}/${name}` : '')
