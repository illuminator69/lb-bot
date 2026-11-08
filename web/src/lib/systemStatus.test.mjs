// Worbus P7 (S1, B-310): the System page says nothing of slskd while
// downloads are off. `node --test web/src/lib/systemStatus.test.mjs`.
import test from 'node:test'
import assert from 'node:assert/strict'
import { cardState, fingerprintShown, healthRows, logFilters, settingsCards } from './systemStatus.js'

// What /api/system/health and /api/settings answer on a server that does not
// download (listenbrainz_bot.py `_settings_cards` and the health checks), with
// Spotify not set up.
const HEALTH_OFF = [
  { id: 'navidrome', label: 'Navidrome', ok: true, detail: 'http://navidrome:4533 (200)' },
  { id: 'slskd', label: 'slskd', ok: true, detail: 'downloading is off (SLSKD_API_KEY is not set)' },
  { id: 'downloads', label: 'Downloads folder', ok: true, detail: 'not used: downloading is off' },
  { id: 'paths', label: 'Library path ↔ Navidrome', ok: true, detail: '/music' },
]
const CARDS_OFF = [
  { key: 'navidrome', title: 'Navidrome', ok: true, values: { url: 'http://navidrome:4533' }, fix: '' },
  { key: 'slskd', title: 'Downloading', ok: true, state: 'off', values: { status: 'not set up' },
    fix: 'Downloading is off. To turn it on, set SLSKD_URL and SLSKD_API_KEY for your slskd, mount its downloads folder at /downloads, and restart lb-bot.' },
  { key: 'spotify', title: 'Spotify', ok: false, values: { client_id: 'not set', client_secret: 'not set' },
    fix: 'Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET for playlist imports.' },
  { key: 'musicbrainz', title: 'MusicBrainz contact', ok: true, values: { contact: '[redacted]' }, fix: '' },
  { key: 'docker_mounts', title: 'Docker mounts', ok: true,
    values: { music: '/music', downloads: '/downloads', config: '/config' }, fix: '' },
]
const LOGS = [['', 'All lines'], ['errors', 'Errors'], ['slskd', 'slskd'], ['navidrome', 'Navidrome']]

// Every word the page would print from these inputs, as the component prints
// them: row labels, details, notes, values, and the status chip.
function words(off) {
  const downloads = !off
  const cards = settingsCards(CARDS_OFF, downloads).map(c => [
    c.title, cardState(c), (c.state === 'off' || !c.ok) && c.fix ? c.fix : '', JSON.stringify(c.values),
  ])
  return JSON.stringify([healthRows(HEALTH_OFF, downloads), cards, logFilters(LOGS, downloads)])
}

test('systemPageDownloadsOffHasNoSlskdWords', () => {
  const text = words(true)
  assert.doesNotMatch(text, /slskd|peer/i)
  assert.doesNotMatch(text, /Check/)
  // The Downloading card is "Off" and nothing else; Spotify unset is "Off".
  const cards = settingsCards(CARDS_OFF, false)
  const dl = cards.find(c => c.key === 'slskd')
  assert.equal(dl.title, 'Downloading')
  assert.equal(cardState(dl), 'Off')
  assert.deepEqual(dl.values, {})
  assert.equal(dl.fix, '')
  assert.equal(cardState(cards.find(c => c.key === 'spotify')), 'Off')
  // Not a failure of a real check: a genuinely unset contact still reads Check.
  const mb = settingsCards([{ ...CARDS_OFF[3], ok: false }], false)[0]
  assert.equal(cardState(mb), 'Check')
  assert.equal(fingerprintShown(false), false)
})

test('systemPageDownloadsOnKeepsSlskdRow', () => {
  assert.deepEqual(healthRows(HEALTH_OFF, true), HEALTH_OFF)
  assert.deepEqual(settingsCards(CARDS_OFF, true), CARDS_OFF)
  assert.deepEqual(logFilters(LOGS, true), LOGS)
  assert.equal(fingerprintShown(true), true)
  assert.equal(cardState(settingsCards(CARDS_OFF, true).find(c => c.key === 'spotify')), 'Check')
  assert.match(words(false), /slskd/)
  assert.match(words(false), /Check/)
})
