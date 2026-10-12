import { useRef, useState } from 'react'

// Album art with a tonal gradient fallback. Prefers building the backend
// cover-proxy URL from albumId (GET /api/cover/{albumId}?size=…, 204 when
// absent — which surfaces here as an empty image load); a raw `url` prop
// still wins when the payload already carries one.
// `fluid` makes the cover fill its container as a square (for tile grids);
// `round` renders it as a circle (artist portraits); `initials` writes the
// name's first two letters on the fallback, for a portrait nobody has.
// `fallbackUrl` is tried once before the gradient — a release-level cover that
// the Archive doesn't hold should show the release-group's sleeve, not a
// coloured rectangle.
export default function Cover({ albumId, url, fallbackUrl, name = '', size = 48, fluid = false, round = false, initials = false }) {
  const hint = fluid ? 300 : size <= 64 ? 64 : 300
  const primary = url || (albumId ? `/api/cover/${encodeURIComponent(albumId)}?size=${hint}` : null)
  const chain = [primary, fallbackUrl && fallbackUrl !== primary ? fallbackUrl : null].filter(Boolean)
  // Sources exhausted, *for this chain*: a count carried over from an earlier
  // url (a 204 on the Navidrome cover before the artist image arrived) skipped
  // the new url and left the gradient up for good.
  const chainKey = chain.join('\n')
  const [fail, setFail] = useState({ key: chainKey, n: 0 })
  const failed = fail.key === chainKey ? fail.n : 0
  const setFailed = () => setFail(f => ({ key: chainKey, n: (f.key === chainKey ? f.n : 0) + 1 }))
  const src = chain[failed] || null
  // The image that last finished loading stays underneath while another one
  // loads, and the new one fades in over it. Switching an album's edition used
  // to blank the cover to its gradient for as long as the next image took.
  const [loaded, setLoaded] = useState(null)
  const under = useRef(null)
  if (loaded && loaded !== src && under.current !== loaded) under.current = loaded
  const showImg = !!src
  // Deterministic hue from the name so fallbacks vary pleasantly.
  let hue = 0
  for (let i = 0; i < name.length; i++) hue = (hue * 31 + name.charCodeAt(i)) % 360
  const letters = initials ? (name.replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2).toUpperCase()) : ''
  return (
    <div
      className={`relative shrink-0 overflow-hidden ${round ? 'rounded-pill' : 'rounded-md'}`}
      style={{
        ...(fluid ? { width: '100%', aspectRatio: '1 / 1' } : { width: size, height: size }),
        background: `linear-gradient(135deg, hsl(${hue} 35% 35%), hsl(${(hue + 50) % 360} 30% 20%))`,
      }}
    >
      {letters && (
        <span aria-hidden="true" className="absolute inset-0 grid place-items-center font-semibold text-white"
          style={{ fontSize: fluid ? '1.6em' : Math.max(11, Math.round(size * 0.3)), opacity: 0.85 }}>{letters}</span>
      )}
      {under.current && under.current !== src && (
        <img src={under.current} alt="" className="absolute inset-0 h-full w-full object-cover" />
      )}
      {showImg && (
        // Lazy + async: a discography is 50-100 covers, pointed straight at
        // coverartarchive.org, which answers with a redirect to archive.org and
        // is routinely slow. Eagerly loading every one queued them all against
        // the browser's 6-per-host limit and stalled the covers on screen.
        <img key={src} src={src} alt="" width={size} height={size}
          loading="lazy" decoding="async"
          className="relative h-full w-full object-cover"
          style={{ opacity: loaded === src ? 1 : 0, transition: 'opacity .28s ease' }}
          onError={setFailed}
          onLoad={e => { if (!e.target.naturalWidth) setFailed(); else setLoaded(src) }} />
      )}
    </div>
  )
}
