import { useState } from 'react'

// Album art with a tonal gradient fallback. Prefers building the backend
// cover-proxy URL from albumId (GET /api/cover/{albumId}?size=…, 204 when
// absent — which surfaces here as an empty image load); a raw `url` prop
// still wins when the payload already carries one.
// `fluid` makes the cover fill its container as a square (for tile grids);
// `round` renders it as a circle (artist portraits).
// `fallbackUrl` is tried once before the gradient — a release-level cover that
// the Archive doesn't hold should show the release-group's sleeve, not a
// coloured rectangle.
export default function Cover({ albumId, url, fallbackUrl, name = '', size = 48, fluid = false, round = false }) {
  const [failed, setFailed] = useState(0)   // count of sources exhausted
  const hint = fluid ? 300 : size <= 64 ? 64 : 300
  const primary = url || (albumId ? `/api/cover/${encodeURIComponent(albumId)}?size=${hint}` : null)
  const chain = [primary, fallbackUrl && fallbackUrl !== primary ? fallbackUrl : null].filter(Boolean)
  const src = chain[failed] || null
  const showImg = !!src
  // Deterministic hue from the album name so fallbacks vary pleasantly.
  let hue = 0
  for (let i = 0; i < name.length; i++) hue = (hue * 31 + name.charCodeAt(i)) % 360
  return (
    <div
      className={`shrink-0 overflow-hidden ${round ? 'rounded-pill' : 'rounded-md'}`}
      style={{
        ...(fluid ? { width: '100%', aspectRatio: '1 / 1' } : { width: size, height: size }),
        background: `linear-gradient(135deg, hsl(${hue} 35% 35%), hsl(${(hue + 50) % 360} 30% 20%))`,
      }}
    >
      {showImg && (
        // Lazy + async: a discography grid is 50-100 covers, and the Artist
        // screen points them straight at coverartarchive.org, which answers
        // with a redirect to archive.org and is routinely slow. Eagerly loading
        // every tile queued them all against the browser's 6-per-host limit and
        // stalled the covers actually on screen. Off-screen tiles now wait.
        <img key={src} src={src} alt="" width={size} height={size}
          loading="lazy" decoding="async"
          className="h-full w-full object-cover"
          onError={() => setFailed(n => n + 1)}
          onLoad={e => { if (!e.target.naturalWidth) setFailed(n => n + 1) }} />
      )}
    </div>
  )
}
