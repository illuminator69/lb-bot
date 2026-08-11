import { useEffect, useMemo, useState } from 'react'
import { useApp, navigate } from '../App.jsx'
import { api } from '../lib/api.js'
import Cover from '../components/Cover.jsx'
import { Chip, EmptyState, PageTitle, Skeleton, SortToggle } from '../components/ui.jsx'

const DAYS = [[7, '7 days'], [30, '30 days'], [90, '90 days']]
const SORTS = [['date', 'Newest'], ['artist', 'Artist A–Z']]
const TYPES = [['all', 'All types'], ['album', 'Albums'], ['ep', 'EPs'], ['single', 'Singles'], ['other', 'Other']]

// Which type bucket a release falls into, from its MusicBrainz primary type.
// Anything that isn't a plain Album/EP/Single (broadcasts, compilations,
// untyped rows) lands in "Other" so nothing silently disappears from a filter.
function typeBucket(r) {
  const t = String(r.type || '').toLowerCase()
  if (t === 'album') return 'album'
  if (t === 'ep') return 'ep'
  if (t === 'single') return 'single'
  return 'other'
}

// Restore a saved Fresh preference so scope/sort/window/type survive tab
// switches and reloads (the mock treats these as sticky filters).
function stored(key, fallback) {
  const v = localStorage.getItem('fresh.' + key)
  return v == null ? fallback : v
}

function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso + 'T00:00:00')
  if (isNaN(d)) return iso
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

// Which weekly bucket a release falls into, by whole days between its release
// date and today. Future dates land in "Upcoming"; everything undated sinks to
// "Older" so it still renders somewhere.
function ageBucket(iso) {
  if (!iso) return 4
  const d = new Date(iso + 'T00:00:00')
  if (isNaN(d)) return 4
  const days = Math.floor((Date.now() - d.getTime()) / 86400000)
  if (days < 0) return -1     // upcoming
  if (days < 7) return 0      // this week
  if (days < 14) return 1     // last week
  if (days < 21) return 2     // two weeks ago
  if (days < 28) return 3     // three weeks ago
  return 4                    // older
}

const BUCKET_LABEL = {
  '-1': 'Upcoming',
  0: 'This week',
  1: 'Last week',
  2: 'Two weeks ago',
  3: 'Three weeks ago',
  4: 'Earlier',
}

// One fresh release: cover + name → the artist's page (owned page when we have
// it, else a MusicBrainz browse page), plus a one-click Get that downloads the
// album through the normal album pipeline (auto source, with failover).
function FreshCard({ r, onOpenArtist, onGet }) {
  const [busy, setBusy] = useState(false)
  const [got, setGot] = useState(false)
  const upcoming = r.releaseDate && r.releaseDate > new Date().toISOString().slice(0, 10)

  async function get() {
    setBusy(true)
    try { await onGet(r); setGot(true) } finally { setBusy(false) }
  }

  return (
    <div className="flex flex-col rounded-[12px] border border-line bg-panel p-2.5">
      <button className="!block !border-0 !bg-transparent !p-0 text-left" onClick={() => onOpenArtist(r)}
        title={`Open ${r.artist}`}>
        <div className="mb-2"><Cover url={r.coverUrl} name={r.releaseName} fluid /></div>
        <div className="truncate text-[13px] font-semibold">{r.releaseName}</div>
        <div className="muted truncate text-[11.5px]">{r.artist}</div>
      </button>
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-faint">
        <span>{fmtDate(r.releaseDate)}</span>
        {r.type ? <span>· {r.type}</span> : null}
        {/* Only the exact release being on disk earns "in library" — an owned
            artist with a brand-new album is still a download. */}
        {r.releaseOwned ? <span className="chip good !my-0 !py-0">in library</span> : null}
        {upcoming ? <span className="chip !my-0 !py-0">upcoming</span> : null}
      </div>
      {r.releaseOwned ? (
        // Already in the library — don't offer a duplicate download; send them
        // to the artist page instead.
        <button className="mt-2.5 !py-1 !text-[12.5px]" onClick={() => onOpenArtist(r)}>
          View in library →
        </button>
      ) : (
        <button className="primary mt-2.5 !py-1 !text-[12.5px]"
          disabled={busy || got || !r.releaseGroupMbid || upcoming}
          aria-busy={busy}
          title={upcoming ? 'Not released yet' : ''}
          onClick={get}>
          {got ? '✓ Queued — see Downloads' : busy ? 'Queuing…' : 'Get this album →'}
        </button>
      )}
    </div>
  )
}

export default function Fresh() {
  const { action, dispatch, pushToast } = useApp()
  const [days, setDays] = useState(() => Number(stored('days', 30)))
  const [scope, setScope] = useState(() => stored('scope', 'yours'))   // 'yours' | 'all'
  const [sort, setSort] = useState(() => stored('sort', 'date'))       // 'date' | 'artist'
  const [type, setType] = useState(() => stored('type', 'all'))        // all|album|ep|single|other
  const [data, setData] = useState(null)         // { releases } | null
  const [error, setError] = useState(null)

  // Persist the sticky filters whenever they change.
  useEffect(() => { localStorage.setItem('fresh.days', String(days)) }, [days])
  useEffect(() => { localStorage.setItem('fresh.scope', scope) }, [scope])
  useEffect(() => { localStorage.setItem('fresh.sort', sort) }, [sort])
  useEffect(() => { localStorage.setItem('fresh.type', type) }, [type])

  useEffect(() => {
    let dead = false
    setData(null); setError(null)
    api('/api/fresh-releases?days=' + days)
      .then(r => !dead && setData(r))
      .catch(e => !dead && setError(e.message))
    return () => { dead = true }
  }, [days])

  const releases = data?.releases || []
  // "Your artists" means artists already in the library — that's artistOwned,
  // NOT releaseOwned (which is only the exact album being on disk).
  const ownedCount = useMemo(() => releases.filter(r => r.artistOwned).length, [releases])
  const scoped = scope === 'yours' ? releases.filter(r => r.artistOwned) : releases
  const typeCounts = useMemo(() => {
    const c = { all: scoped.length, album: 0, ep: 0, single: 0, other: 0 }
    for (const r of scoped) c[typeBucket(r)]++
    return c
  }, [scoped])
  const shown = type === 'all' ? scoped : scoped.filter(r => typeBucket(r) === type)

  // Sort by artist → one flat A–Z list; sort by date → ordered weekly buckets
  // with divider headers so "this week" is visually separated from "earlier".
  const groups = useMemo(() => {
    if (sort === 'artist') {
      const sorted = [...shown].sort((a, b) =>
        (a.artist || '').localeCompare(b.artist || '') ||
        (a.releaseName || '').localeCompare(b.releaseName || ''))
      return sorted.length ? [{ key: 'all', label: null, items: sorted }] : []
    }
    const byBucket = new Map()
    for (const r of shown) {
      const b = ageBucket(r.releaseDate)
      if (!byBucket.has(b)) byBucket.set(b, [])
      byBucket.get(b).push(r)
    }
    return [-1, 0, 1, 2, 3, 4]
      .filter(b => byBucket.has(b))
      .map(b => ({
        key: String(b),
        label: BUCKET_LABEL[b],
        items: byBucket.get(b).sort((a, c) => (c.releaseDate || '').localeCompare(a.releaseDate || '')),
      }))
  }, [shown, sort])

  function openArtist(r) {
    const id = r.artistOwned && r.artistId
      ? r.artistId
      : (r.artistMbids?.[0] ? 'mb:' + r.artistMbids[0] : null)
    if (!id) { pushToast('No MusicBrainz artist for this release', 'info'); return }
    navigate('Artist', id)
  }

  async function getAlbum(r) {
    try {
      await action('/api/album/download', { rgid: r.releaseGroupMbid })
      pushToast(`Queued ${r.releaseName} — see Downloads`)
    } catch (e) {
      pushToast(`Download failed: ${e.message}`, 'error')
      throw e
    }
  }

  return (
    <>
      <div className="mb-[18px] flex flex-wrap items-end gap-4">
        <PageTitle eyebrow="Fresh" title="New releases" />
        <span className="spacer" />
        <div className="flex items-center gap-1.5">
          <Chip active={scope === 'yours'} count={ownedCount} onClick={() => setScope('yours')}>Your artists</Chip>
          <Chip active={scope === 'all'} count={releases.length} onClick={() => setScope('all')}>All</Chip>
        </div>
        <div className="flex items-center gap-1.5">
          {DAYS.map(([d, label]) => (
            <Chip key={d} variant="solid" active={days === d} onClick={() => setDays(d)}>{label}</Chip>
          ))}
        </div>
        <SortToggle value={sort} options={SORTS} onChange={setSort} />
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-1.5">
        {TYPES.map(([k, label]) => (
          <Chip key={k} active={type === k} count={typeCounts[k]} onClick={() => setType(k)}>{label}</Chip>
        ))}
      </div>
      <p className="muted mb-4 text-[12.5px]">
        Recent and upcoming releases from ListenBrainz. “Your artists” shows only artists already in your library.
      </p>

      {error ? (
        <EmptyState title="Couldn't load fresh releases" hint={error} />
      ) : data === null ? (
        <div className="tile-grid">
          {Array.from({ length: 12 }, (_, i) => (
            <div key={i} className="rounded-[12px] border border-line bg-panel p-2.5">
              <Skeleton style={{ width: '100%', aspectRatio: '1 / 1' }} />
              <Skeleton className="mt-2 h-3 w-3/5" />
              <Skeleton className="mt-1.5 h-2.5 w-2/5" />
            </div>
          ))}
        </div>
      ) : !shown.length ? (
        <EmptyState
          title={type !== 'all' && scoped.length
            ? `No ${TYPES.find(t => t[0] === type)?.[1].toLowerCase() || 'releases'} here`
            : scope === 'yours' ? 'No fresh releases from your artists' : 'No fresh releases in this window'}
          hint={type !== 'all' && scoped.length
            ? 'Nothing of this type in the current scope — clear the type filter to see the rest.'
            : scope === 'yours'
              ? 'None of your library artists have a release in this window — widen the range or see everyone.'
              : 'Try a wider date range.'}>
          {type !== 'all' && scoped.length > 0 ? (
            <button className="primary" onClick={() => setType('all')}>Show all types</button>
          ) : scope === 'yours' && releases.length > 0 && (
            <button className="primary" onClick={() => setScope('all')}>See all new releases</button>
          )}
        </EmptyState>
      ) : (
        groups.map(g => (
          <div key={g.key} className="mb-6">
            {g.label && (
              <div className="mb-2.5 flex items-center gap-2.5">
                <div className="text-[12px] font-semibold uppercase tracking-[.1em]" style={{ color: 'var(--text2)' }}>
                  {g.label}
                </div>
                <span className="h-px flex-1" style={{ background: 'var(--border)' }} />
                <span className="text-[11.5px] text-faint">{g.items.length}</span>
              </div>
            )}
            <div className="tile-grid">
              {g.items.map(r => (
                <FreshCard key={r.releaseMbid || r.releaseGroupMbid} r={r}
                  onOpenArtist={openArtist} onGet={getAlbum} />
              ))}
            </div>
          </div>
        ))
      )}
    </>
  )
}
