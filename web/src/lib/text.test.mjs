// A name put into a sentence is isolated, so that a right-to-left one cannot
// reorder the words around it (I-029, Z1). `node --test web/src/lib/text.test.mjs`.
import test from 'node:test'
import assert from 'node:assert/strict'
import { iso } from './text.js'

const FSI = '\u2068', PDI = '\u2069'

test('a name is wrapped in FIRST STRONG ISOLATE … POP DIRECTIONAL ISOLATE, whatever its script', () => {
  assert.equal(iso("להקת הדוגמה"), `${FSI}להקת הדוגמה${PDI}`)
  assert.equal(iso('The Example Band'), `${FSI}The Example Band${PDI}`)
  assert.equal(iso('1986オメガトライブ'), `${FSI}1986オメガトライブ${PDI}`)
  assert.equal(iso("להקת הדוגמה").length, "להקת הדוגמה".length + 2, 'two characters, and nothing of the name is touched')
})

test('no name, nothing: an empty one puts no stray mark into the sentence', () => {
  for (const none of ['', null, undefined]) assert.equal(iso(none), '')
})

test('what is not a string is the string it prints as', () => {
  assert.equal(iso(1986), `${FSI}1986${PDI}`)
})
