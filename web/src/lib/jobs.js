// The app's own job watch (lib/jobWatch.js), and how a component reads it.
// One instance for the whole app: the album page, the To fix panes and
// System › Overrides all follow a job through it, so it is polled once.
import { useEffect, useRef, useSyncExternalStore } from 'react'
import { api } from './api.js'
import { scanMachine } from './artistMatch.js'
import { createJobWatch } from './jobWatch.js'
import { mergeMachine } from './merges.js'

// GET /api/album/override?id= — what a job's poll reads. The answer describes
// the row's album too (its current id, the release its files carry), which is
// what the album page shows when the job ends.
//
// With a timeout: a connection that stalls would otherwise hold the poll for
// as long as the browser cares to wait — no further poll, and no "Still
// working". A read that timed out is no answer, like any that failed.
const POLL_TIMEOUT_MS = 15_000

async function readAlbumJob(id) {
  try {
    const album = await api('/api/album/override?id=' + encodeURIComponent(id), { timeoutMs: POLL_TIMEOUT_MS })
    return album?.override ? { ok: true, row: album.override, album } : { ok: false, status: 200, code: '' }
  } catch (e) {
    return { ok: false, status: e.status || 0, code: e.payload?.code || '' }
  }
}

// GET /api/tasks/<id> — the discography scan that follows an artist match.
// The job is known by the artist's id; its state names the task to ask about.
async function readScan(_artistId, state) {
  try {
    return { ok: true, task: await api('/api/tasks/' + encodeURIComponent(state.taskId), { timeoutMs: POLL_TIMEOUT_MS }) }
  } catch (e) {
    return { ok: false, status: e.status || 0 }
  }
}

// GET /api/artist/merge?id= — an artist name merge, or its undo: the row.
async function readMerge(id) {
  try {
    const row = await api('/api/artist/merge?id=' + encodeURIComponent(id), { timeoutMs: POLL_TIMEOUT_MS })
    return row?.id ? { ok: true, row } : { ok: false, status: 200, code: '' }
  } catch (e) {
    return { ok: false, status: e.status || 0, code: e.payload?.code || '' }
  }
}

export const jobs = createJobWatch({
  readers: { album: readAlbumJob, scan: readScan, merge: readMerge },
  machines: { scan: scanMachine, merge: mergeMachine },
})

/** The jobs with these ids, as `{ [id]: job }`, for a surface that shows them.
 *  While it is mounted the surface counts as showing each of them: an ended
 *  job stays on it, and a job that ends after it is gone is a toast instead. */
export function useJobs(kind, ids) {
  useSyncExternalStore(jobs.subscribe, jobs.version)
  // Attached one id at a time, never all dropped and taken again: letting go
  // of the last hold on an ended job is what forgets it.
  const held = useRef(new Map())
  const key = ids.join('\n')
  useEffect(() => {
    const want = new Set(ids)
    for (const id of want) if (!held.current.has(id)) held.current.set(id, jobs.attach(kind, id))
    for (const [id, detach] of held.current) {
      if (!want.has(id)) { detach(); held.current.delete(id) }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, key])
  useEffect(() => () => {
    for (const detach of held.current.values()) detach()
    held.current.clear()
  }, [])
  const out = {}
  for (const id of ids) {
    const job = jobs.get(kind, id)
    if (job) out[id] = job
  }
  return out
}
