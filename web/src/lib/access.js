// Worbus P1 (S6): what the signed-in user may see, and what this server does.
//
// - `downloads` comes from `/api/summary` as "on" | "off"; absent from a
//   loaded summary is an older lb-bot, which always downloads ("on"). Before
//   the summary has loaded it is unknown: download surfaces stay hidden (no
//   first-paint flash of them on a server that does not download), and no
//   route is redirected for it (`routeDownloads`).
// - An admin is a Navidrome admin (`GET /api/session`). The routes that change
//   files are admin-only on the server; a non-admin's pages leave their
//   controls out altogether (absent, not disabled).
// - A download control needs both: an admin, on a server that downloads.

/** Whether this server downloads. Not loaded yet (null/undefined) is not
 *  known, so not on; absent from a loaded summary (an older lb-bot) is on. */
export const downloadsOn = summary => summary != null && summary.downloads !== 'off'

/** Whether the summary has said yet. */
export const downloadsKnown = summary => summary != null

/** `downloads` for routing: while unknown a route is left where it is (it
 *  would otherwise bounce off Activity on every load), so unknown reads on. */
export const routeDownloads = summary => !downloadsKnown(summary) || downloadsOn(summary)

/** The sidebar's sections for this user and server. */
export function sectionsFor(sections, { admin, downloads }) {
  return sections.filter(([k]) => {
    if (!admin) return k === 'discover' || k === 'library'
    if (!downloads) return k !== 'activity'
    return true
  })
}

/** Where a route goes when this user or server cannot show it: null when it
 *  can be shown. System is the one section a non-admin keeps a page of
 *  (Appearance, a per-browser setting). */
export function routeRedirect(tab, sub, { admin, downloads }) {
  if (!admin) {
    if (['needs', 'activity', 'place'].includes(tab)) return ['library']
    if (tab === 'system' && sub !== 'appearance') return ['system', 'appearance']
    return null
  }
  if (!downloads) {
    if (tab === 'activity' || tab === 'place') return ['needs']
    if (tab === 'system' && sub === 'sources') return ['system', 'status']
  }
  return null
}

// Find it (S4): the services' names, for "Search <service>".
const SERVICE_NAMES = {
  bandcamp: 'Bandcamp', qobuz: 'Qobuz', 'apple-music': 'Apple Music', spotify: 'Spotify',
  deezer: 'Deezer', 'youtube-music': 'YouTube Music', tidal: 'TIDAL', amazon: 'Amazon',
  beatport: 'Beatport', soundcloud: 'SoundCloud',
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return '' }
}

/** `findIt` rows as the album page lists them: confirmed links first, each
 *  named by its service (an `other` one by its host), then the searches,
 *  each "Search <service>". Rows that are not https or http links are left
 *  out; null when the server sent none (the section says it could not load). */
export function findItRows(findIt) {
  if (!Array.isArray(findIt)) return null
  const ok = findIt.filter(l => l && typeof l.url === 'string' && /^https?:\/\//i.test(l.url))
  const name = l => SERVICE_NAMES[l.service] || hostOf(l.url) || 'Link'
  const confirmed = ok.filter(l => l.kind === 'confirmed').map(l => ({ ...l, label: name(l) }))
  const search = ok.filter(l => l.kind === 'search').map(l => ({ ...l, label: `Search ${name(l)}` }))
  return [...confirmed, ...search]
}
