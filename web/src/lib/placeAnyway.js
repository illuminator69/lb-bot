// What Fill gaps says before "Place anyway" places a file lb-bot refused
// (B-087, J5). Pure, so the wording has a state matrix: placeAnyway.test.mjs.
//
// The confirmation used to say "…so this will add a second copy" for every
// offer. There are two kinds now, and the row says which (`forcePlaceKind`):
// `duplicate` — the same audio is already in the album — and `recording` —
// lb-bot's identity check (AcoustID's fingerprint, or the file's own id tags)
// found the audio to be another recording than the track's. The button
// overrules the refusal the row shows and no other, so the confirmation says
// that one.
export function placeAnywayConfirm(track) {
  const ask = 'Place this file anyway?\n\n'
  if (track?.forcePlaceKind === 'recording') {
    // The row's own text is what was found: lb-bot's words, not rebuilt here.
    const found = String(track.downloadError || '').replace(/^(not placed|placement failed): /, '')
    return ask
      + (found ? `lb-bot’s check refused it: ${found}.` : 'lb-bot’s check found this audio to be another recording than this track.')
      + `\n\nThe file will be filed as “${track.title || 'this track'}” anyway.`
  }
  return ask + 'The album already contains the same audio as '
    + `${track?.forcePlaceConflict || 'another file'}, so this will add a second copy.`
}

// What the press sends with it (B-087, J19): the row the page drew — its
// recording id and its title. The route takes the row's index, and a rescan
// between the drawing and the press can put another row at that index, one
// with an offer of its own; lb-bot then answers 409 `row_moved` instead of
// placing that row's file after a confirmation that quoted this one's refusal.
export function placeAnywayBody(track) {
  return { recordingMbid: track?.recordingMbid || '', title: track?.title || '' }
}

// What the page says of a press that was not taken. `row_moved` is lb-bot's
// own sentence and says what to do, so it stands alone; anything else keeps
// the page's lead-in.
export function placeAnywayFailure(err) {
  if (err?.payload?.code === 'row_moved') return err.message
  return `Place anyway failed: ${err?.message}`
}
