export async function api(path, opts = {}) {
  // `timeoutMs` is opt-in, not a default: some routes here deliberately run for
  // minutes (scans, source searches). It exists for the read-only lookups a
  // screen *blocks on* — a MusicBrainz call that never answers used to leave
  // the album page on skeletons with its buttons disabled forever, because
  // fetch on its own has no timeout at all.
  const { timeoutMs, ...init } = opts
  const controller = timeoutMs ? new AbortController() : null
  const timer = controller
    ? setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs)
    : null
  let res
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...(controller ? { signal: controller.signal } : {}),
      ...init,
    })
  } catch (e) {
    // A caller that aborted us is the only reason to claim a timeout: an
    // upstream `signal` in `init` aborting is the caller's own cancellation.
    if (controller?.signal.aborted && !init.signal?.aborted) {
      const err = new Error(`Timed out after ${Math.round(timeoutMs / 1000)}s`)
      err.timeout = true
      throw err
    }
    throw e
  } finally {
    if (timer) clearTimeout(timer)
  }
  const text = await res.text()
  let data
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    // Non-JSON body (e.g. an HTML error page from a proxy, or a crash before
    // the server's JSON error handler runs). Surface the HTTP status instead
    // of a cryptic "Unexpected token '<'" parser error.
    const err = new Error(res.ok
      ? 'Server returned a non-JSON response'
      : `HTTP ${res.status} ${res.statusText}`)
    err.status = res.status
    throw err
  }
  if (!res.ok) {
    const err = new Error(data.reason || data.error || data.message || res.statusText)
    // Structured error envelope ({code, reason, detail, nextSource, logTail})
    // — failure cards render this when present.
    err.payload = data
    err.status = res.status
    throw err
  }
  return data
}

export const get = (path) => api(path)

export const post = (path, body = {}) =>
  api(path, { method: 'POST', body: JSON.stringify(body) })

export const put = (path, body = {}) =>
  api(path, { method: 'PUT', body: JSON.stringify(body) })
