// Run: node --test web/src/lib/source.test.mjs   (no dependency, no network)
// The source holds no invisible character (I-029, T1). A no-break space, a
// zero-width space, a directional mark or isolate typed as the character
// itself cannot be seen in an editor or a diff, and reads as an ordinary
// space or as nothing. They are written as escapes — which this test does not
// mind — and this file names them by number for the same reason.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..')
const TEXT = /\.(js|jsx|mjs|css|html)$/

/** The code points that may not stand in the source as themselves. */
export const INVISIBLE = [
  0x00a0,                                   // no-break space
  0x200b,                                   // zero-width space
  0x200e, 0x200f,                           // left-to-right mark, right-to-left mark
  0x202a, 0x202b, 0x202c, 0x202d, 0x202e,   // the directional embeddings and overrides, and their pop
  0x2066, 0x2067, 0x2068, 0x2069,           // the isolates, and their pop
  0xfeff,                                   // zero-width no-break space (a byte-order mark)
]

/** Every invisible character in `text`: [{ line, column, code }]. */
export function invisibles(text) {
  const found = []
  const lines = String(text).split('\n')
  for (let i = 0; i < lines.length; i++) {
    for (let j = 0; j < lines[i].length; j++) {
      const code = lines[i].charCodeAt(j)
      if (INVISIBLE.includes(code)) found.push({ line: i + 1, column: j + 1, code: 'U+' + code.toString(16).toUpperCase().padStart(4, '0') })
    }
  }
  return found
}

function files(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...files(path))
    else if (TEXT.test(name)) out.push(path)
  }
  return out
}

test('the finder sees each of them, where it stands — and not its escape', () => {
  for (const code of INVISIBLE) {
    const text = 'one line\nab' + String.fromCharCode(code) + 'cd\n'
    assert.deepEqual(invisibles(text), [{ line: 2, column: 3, code: 'U+' + code.toString(16).toUpperCase().padStart(4, '0') }])
  }
  assert.deepEqual(invisibles("const nb = '\\u00a0' // and \\u2068 … \\u2069\n"), [], 'an escape is six ordinary characters')
  assert.deepEqual(invisibles('plain text, “quotes”, an arrow → and a dot ·\n'), [])
})

test('no file under web/src holds an invisible character typed as itself', () => {
  const all = files(SRC)
  assert.ok(all.length > 20, `the walk found the source (${all.length} files)`)
  const bad = []
  for (const path of all) {
    for (const hit of invisibles(readFileSync(path, 'utf8'))) bad.push(`${relative(SRC, path)}:${hit.line}:${hit.column} ${hit.code}`)
  }
  assert.deepEqual(bad, [], 'write it as an escape (\\u00a0, \\u2068 …): ' + bad.slice(0, 12).join(', '))
})
