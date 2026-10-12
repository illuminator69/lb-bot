// What lb-bot's own web UI may say about the library index, from the `index`
// block of /api/summary (ruling U1). ONE function for the header and for the
// Settings card, so the two cannot show different numbers for one thing.
//
// Returns null when there is nothing measured to show: no block, the worker
// off, or its numbers not there yet (it has not read the artist list). Never
// "0 of 0".
//
//   kind      in the header   what it means
//   build     yes             a manual "Build library index" task is running
//   indexing  yes             the worker has artists it can scan now
//   outage    yes             the worker stopped: MusicBrainz is not answering
//   waiting   no              all that is left failed and waits for a retry
//   done      no              every artist is up to date
//
// `done` of `total` is, for the worker, the library artists that are UP TO
// DATE by its own rule (scanned with this version of the matcher, inside the
// rescan interval); for a build it is the task's own count of artists walked.
export function indexProgress(index) {
  if (!index) return null
  const build = index.build
  if (build) {
    const total = Math.max(0, Math.floor(Number(build.total) || 0))
    const done = Math.min(Math.max(0, Math.floor(Number(build.done) || 0)), total)
    return {
      kind: 'build', done, total, remaining: total - done,
      pct: total ? (done / total) * 100 : 0, current: build.current || '', pausedUntil: 0,
    }
  }
  if (index.enabled === false || !index.running) return null
  const { total, remaining } = index
  if (!Number.isInteger(total) || !Number.isInteger(remaining) || total <= 0) return null
  const left = Math.min(Math.max(0, remaining), total)
  const done = total - left
  const kind = left === 0 ? 'done'
    : index.outcome === 'outage' ? 'outage'
    : index.outcome === 'idle' ? 'waiting'
    : 'indexing'
  return {
    kind, done, total, remaining: left, pct: (done / total) * 100,
    current: kind === 'indexing' ? (index.current || '') : '',
    pausedUntil: kind === 'outage' ? Number(index.pausedUntil) || 0 : 0,
  }
}

// The header's words for it: [long, short], or null when the header shows
// nothing. `nowSecs` and `fmt` are passed in so this stays a pure function.
export function indexHeaderLabel(p, nowSecs, fmt = n => n.toLocaleString(), clock = t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })) {
  if (!p || p.kind === 'waiting' || p.kind === 'done') return null
  if (p.kind === 'build') {
    return p.total
      ? [`Building index · ${fmt(p.done)} of ${fmt(p.total)}`, `Building · ${fmt(p.done)}/${fmt(p.total)}`]
      : ['Building index', 'Building index']
  }
  if (p.kind === 'outage') {
    const until = p.pausedUntil > nowSecs ? ` · resumes ${clock(p.pausedUntil)}` : ''
    return [`Indexing paused · MusicBrainz is not answering${until}`, `Indexing paused${until}`]
  }
  return [`Indexing library · ${fmt(p.done)} of ${fmt(p.total)} artists`, `Indexing · ${fmt(p.done)}/${fmt(p.total)}`]
}
