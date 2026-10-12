// What the album page may say about a click on a source, about where a fill is
// coming from, and about the list the click was made in (ruling A1). Pure, so
// the wording has a state matrix: albumSources.test.mjs.
//
// The page used to say "Queued from @<the clicked peer>" the moment the POST
// came back — before anything was queued, and whatever lb-bot then downloaded
// from. A click now takes that folder out of the list that was shown, and the
// fill says which peer it has (`source`) beside the one that was clicked
// (`pickedSource`), so the page can say both.

// The toast after POST /api/album/download with a source named. `existing` is a
// fill already in flight for the release: it is the answer, and the click did
// not change where it comes from.
//
// `all` is given for "Download all" (`{ allFiles: true, fileCount }`): the
// answer echoes what lb-bot understood (`allFiles`), and only that echo lets
// the page say "all files". Asked for and not echoed — lb-bot, or something
// between it and the page, did not take it — it says so.
export function askedText(answer, peer, all) {
  if (answer?.existing) return 'This album is already being downloaded — the source was not changed'
  if (all?.allFiles) {
    if (answer?.allFiles === true) return `Asked @${peer} for all ${all.fileCount} files in the folder`
    return 'lb-bot is downloading the album’s tracks only — it did not take “all files”'
  }
  return `Asked @${peer} for this album`
}

// The quiet line of a row whose folder has more files than a plain download
// asks for, or '' — `extraFiles` is 0, or absent (no tracklist to count
// against, an older lb-bot). The server's field and nothing else: it is the
// rule the download itself asks by, which the files on the screen do not show.
export function extraFilesText(src) {
  const n = Number(src?.extraFiles)
  if (!(n > 0)) return ''
  return n === 1 ? '1 more file in this folder' : `${n} more files in this folder`
}

// The label of the row's second, quieter action — every audio file of the
// folder — or '' when there is nothing more to ask for.
export function downloadAllLabel(src) {
  return extraFilesText(src) ? `Download all ${src.fileCount} files` : ''
}

// One phrase for the files a fill did not ask its source for, or ''.
export function leftOutText(fill) {
  const n = Number(fill?.leftOut)
  if (!(n > 0)) return ''
  return n === 1 ? '1 other file in the folder was left out' : `${n} other files in the folder were left out`
}

// One phrase for where a fill is coming from, or '' when nothing says.
// `source` is the peer the fill has now; `pickedSource` the peer that was
// clicked ('' for a fill lb-bot chose the source of).
export function fillSourceText(fill) {
  const source = fill?.source || ''
  const picked = fill?.pickedSource || ''
  if (source && picked && source !== picked) return `from @${source} — lb-bot moved on from @${picked}, which you picked`
  if (source) return `from @${source}`
  if (picked) return `asked for from @${picked}`
  return ''
}

// What `fillSourceText` says beyond the peer a row already names as its own
// title (the request's row since the 2026-10 redesign is titled "@<the peer
// the fill has>"): that lb-bot moved on from the peer that was picked, or the
// picked peer while none is serving yet. '' when it would only repeat that
// name — the fill is on the peer it began with — or when nothing says.
export function fillSourceBeyond(fill) {
  const source = fill?.source || ''
  const picked = fill?.pickedSource || ''
  if (source && (!picked || picked === source)) return ''
  return fillSourceText(fill)
}

// How old the list on the screen is. `searchedAt` and `serverTime` are both
// lb-bot's clock (seconds); `receivedMs` is this page's clock when the answer
// arrived and `nowMs` the same clock now, so no two clocks are ever compared.
// The caller passes a `nowMs` that moves (the picker re-renders on a timer):
// "just now" must not stand over a list lb-bot has since let go of.
//
// A list lb-bot answered out of what it kept (`cached`) says so, and no more:
// it may be one this page never showed — the automatic download's own search
// left it, or another client asked for it.
export function sourcesAgeText(meta, nowMs) {
  if (!meta || !(meta.searchedAt > 0)) return ''
  const atServer = Number.isFinite(meta.serverTime) ? meta.serverTime - meta.searchedAt : 0
  const here = Number.isFinite(meta.receivedMs) ? (nowMs - meta.receivedMs) / 1000 : 0
  const secs = Math.max(0, atServer) + Math.max(0, here)
  const ago = secs < 60 ? 'just now' : `${Math.floor(secs / 60)} min ago`
  return meta.cached ? `Kept from a search ${ago}` : `Searched ${ago}`
}

// The line under a source row that says whether it is the album, or null for
// none: `{ ok, text, title }`. By what lb-bot takes for album evidence
// (`albumEvidence`: "name", "tracks" or "" — absent from a server or a list
// that never judged it), not by how the folder's name scores: for a title
// with no word of its own ("After EP") a name that scores 80% is no evidence,
// and the row used to read "✓ matches album title" all the same.
export function albumBadge(src) {
  const score = Number(src?.albumMatch) || 0
  const scored = `The folder name scores ${score}% against the album title`
  const evidence = src?.albumEvidence
  if (evidence === 'name') return { ok: true, text: '✓ matches album title', title: scored }
  if (evidence === 'tracks') {
    return { ok: true, text: '✓ its files are this album’s tracks',
             title: 'The files found in this folder carry the titles of this album’s tracks' }
  }
  if (evidence === '' && src?.albumMatchOk) {
    return { ok: false, text: 'album not confirmed',
             title: `${scored} — but for this title a name alone does not tell its albums apart, `
                    + 'and the files found here do not show this album’s tracks' }
  }
  if (score <= 0) return null
  if (evidence == null && src?.albumMatchOk) return { ok: true, text: '✓ matches album title', title: scored }
  return { ok: false, text: 'different album?', title: scored }
}

// A row's identity across two answers: "Search again" re-ranks the list, so
// its position (`id`) is not one.
export function sourceKey(src) {
  return `${src?.peer || ''}\n${src?.folder || ''}`
}

// Said when every search behind the list ended at slskd's response limit.
export const SAMPLED_NOTE = 'Soulseek stopped this search at its response limit, so this is a sample of who has the album — searching again can find other peers.'
