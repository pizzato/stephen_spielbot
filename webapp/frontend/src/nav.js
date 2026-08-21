// nav.js — the single source of truth mapping every page to a URL and back.
//
// Routing is hash-based (#/...) so deep links, refresh, and back/forward all
// work against the SPA's single index.html with no server-side routing.

import { useState, useEffect } from 'react'
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
  activity: { seg: ['activity'] },
  // remix kept as a deep-link alias → same unified Edit film screen as editfilm.
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
  // Advanced prompt editor — reached from Settings → Infrastructure, not the sidebar.
  prompts: { seg: ['prompts'] },
  // About the app — reached from the sidebar handle and the Home hero card.
  about: { seg: ['about'] },
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
// so a stale or hand-edited URL degrades gracefully instead of breaking. A
// query part (#/films?status=published) belongs to the screen's filters
// (useHashParams below) and is ignored here.
export function parseHash(hash) {
  const segs = (hash || '').replace(/^#/, '').split('?')[0].split('/').filter(Boolean)
  if (!segs.length) return { route: 'home' }
  const route = BY_SEG[segs[0]]
  if (!route) return { route: 'home' }
  const r = ROUTES[route]
  if (r.sub && segs[1] === r.sub) return { route, view: r.sub, name: segs[2] }
  if (r.name) return { route, name: segs[1] }
  return { route }
}

// Screen filter state that lives in the hash's query part
// (#/films?status=published&channel=Kids), so filters accumulate in the URL
// and back/forward restores them. `defaults` names the allowed keys and their
// "no filter" values; default-valued keys are omitted from the URL. Updates
// use history.replaceState — changing a filter refines the current history
// entry rather than minting one per click, and in-app navigation (a fresh
// buildHash) naturally starts the next page unfiltered.
export function useHashParams(defaults) {
  const read = () => {
    const h = window.location.hash
    const qi = h.indexOf('?')
    const sp = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : '')
    const out = { ...defaults }
    for (const k of Object.keys(defaults)) {
      const v = sp.get(k)
      if (v != null) out[k] = v
    }
    return out
  }
  const [params, setParams] = useState(read)
  // Back/forward (and sidebar re-clicks) arrive as hashchange: re-read so the
  // screen's filters follow the URL.
  useEffect(() => {
    const onHash = () => setParams(read())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const update = (patch) => setParams((cur) => {
    const next = { ...cur, ...patch }
    const h = window.location.hash || '#/'
    const qi = h.indexOf('?')
    const path = qi >= 0 ? h.slice(0, qi) : h
    const sp = new URLSearchParams()
    for (const k of Object.keys(defaults)) {
      if (next[k] !== defaults[k] && next[k] !== '' && next[k] != null) sp.set(k, next[k])
    }
    const qs = sp.toString()
    window.history.replaceState(null, '', path + (qs ? '?' + qs : ''))
    return next
  })
  return [params, update]
}

// work_dir ⇄ basename. Every work_dir is a direct child of the videos dir, so
// the basename identifies it uniquely and round-trips losslessly.
export const nameOf = (workDir) => (workDir || '').replace(/\/+$/, '').split('/').pop()
export const pathOf = (name, videosDir) =>
  (name && videosDir ? `${videosDir.replace(/\/+$/, '')}/${name}` : '')
