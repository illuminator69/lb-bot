// Worbus P7 (S1, B-310): what the System page shows of downloading.
//
// A server that does not download (no slskd key) is a mode, not a fault, and
// a stranger who never set up Soulseek must not read about it. While
// downloads are off the page drops the slskd health row, the Downloads folder
// row, the fingerprint row (it checks downloaded files, and counts "peers
// rejected"), the slskd log filter and the slskd words on the Downloading
// card, and a Spotify that is not set up reads "Off" rather than "Check".
// With downloads on, every function returns its input: the page is as it was.
// The component passes the same flag the tabs use (`useApp().downloadsOn`).

/** The status chip of a Configuration card. */
export const cardState = c => (c.state === 'off' ? 'Off' : c.ok ? 'OK' : 'Check')

// Health rows that exist only for downloading (`/api/system/health` ids are
// matched by label, which is what the server sends for both).
const DOWNLOAD_CHECK_LABELS = new Set(['slskd', 'downloads folder'])

/** Connection rows, without the downloading ones while downloads are off. */
export function healthRows(checks, downloads) {
  if (downloads) return checks
  return checks.filter(c => !DOWNLOAD_CHECK_LABELS.has(String(c.label || '').trim().toLowerCase()))
}

/** Configuration cards: while downloads are off, the Downloading card is
 *  "Off" and nothing else, the Docker mounts card leaves out the downloads
 *  mount, and an unset Spotify is "Off". */
export function settingsCards(cards, downloads) {
  if (downloads) return cards
  return cards.map(c => {
    if (c.key === 'slskd') return { ...c, title: 'Downloading', ok: true, state: 'off', values: {}, fix: '' }
    if (c.key === 'spotify' && !c.ok) return { ...c, state: 'off' }
    if (c.key === 'docker_mounts') {
      const { downloads: _unused, ...values } = c.values || {}
      return { ...c, values }
    }
    return c
  })
}

/** The Logs tab's "Show" options, without the slskd one while downloads are off. */
export const logFilters = (filters, downloads) => (downloads ? filters : filters.filter(([k]) => k !== 'slskd'))

/** Whether the audio fingerprint row is shown (it is about downloaded files). */
export const fingerprintShown = downloads => !!downloads
