// B-095: an open tab notices that lb-bot was upgraded under it. A deploy left
// a tab running the old page's script with nothing saying so; the page is
// served no-cache, so a reload always gets the new one. The tab reads
// `build` off GET /api/health (lb-bot's own route, open, never 503) and
// compares it with the build the page was served with — lb-bot writes it
// into index.html (`<meta name="lb-bot-build">`); with none there (an older
// lb-bot, the Vite dev server) the first answer is the baseline. Checked on
// arrival, on focus and when the tab is shown again, at most once a minute;
// a read gets BUILD_READ_TIMEOUT_MS. It says so and offers Reload; it never
// reloads by itself.

export const BUILD_CHECK_GAP_MS = 60_000

export const BUILD_READ_TIMEOUT_MS = 10_000

export const UPDATED_TEXT = 'lb-bot was updated. Reload to use the new version.'

// May a check run now? The first one always may (it sets the baseline).
export function checkDue(lastAt, now, gap = BUILD_CHECK_GAP_MS) {
  return lastAt == null || now - lastAt >= gap
}

// A build id worth comparing: a non-empty string. An older lb-bot (no
// `build`), a failed read or junk says nothing either way.
export function readBuild(body) {
  const b = body && typeof body.build === 'string' ? body.build.trim() : ''
  return b || null
}

// One step: `state` is { baseline, updated, lastAt }, `build` what the read
// answered (null for no answer). Returns the next state. Once updated it stays
// updated: the page is the old one until it is reloaded.
export function buildStep(state, build, now) {
  const next = { ...state, lastAt: now }
  if (!build || state.updated) return next
  if (state.baseline == null) return { ...next, baseline: build }
  if (build !== state.baseline) return { ...next, updated: true }
  return next
}

// The watch, with the read, the clock and the listener hooks passed in so the
// matrix drives it with no browser. `read(signal)` resolves to a build id or
// null; one that has not answered in `timeoutMs` is aborted and is no answer,
// so a hung request never stops the checks after it. `baseline` is the build
// the page was served with (null: the first answer is).
export function createBuildWatch({ read, now = () => Date.now(), onUpdated = () => {},
  baseline = null, timeoutMs = BUILD_READ_TIMEOUT_MS }) {
  let state = { baseline, updated: false, lastAt: null }
  let inFlight = false
  async function check() {
    if (inFlight || state.updated || !checkDue(state.lastAt, now())) return state
    inFlight = true
    state = { ...state, lastAt: now() }
    let build = null
    const ctl = typeof AbortController === 'function' ? new AbortController() : null
    let timer
    const limit = new Promise(resolve => {
      timer = setTimeout(() => { ctl?.abort(); resolve(null) }, timeoutMs)
    })
    try {
      build = await Promise.race([Promise.resolve().then(() => read(ctl?.signal)), limit])
    } catch { build = null } finally { clearTimeout(timer) }
    const was = state.updated
    state = buildStep(state, build, now())
    inFlight = false
    if (state.updated && !was) onUpdated()
    return state
  }
  return { check, get state() { return state } }
}
