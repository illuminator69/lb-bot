export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
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
