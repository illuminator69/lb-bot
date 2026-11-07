// The page's favicon must be a Vite-built asset: Flask serves /assets/ from
// the build and answers every other unknown path with index.html, so an icon
// in web/public/ (or a bare /favicon.ico) would come back as HTML.
// Needs `npm run build` first; with no dist/ it FAILS, it does not skip.
import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const dist = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dist')

test('favicon_is_built_asset', () => {
  const indexPath = join(dist, 'index.html')
  assert.ok(
    existsSync(indexPath),
    `web/dist/index.html is missing: run "npm run build" in web/ before "node --test" (${indexPath})`,
  )
  const html = readFileSync(indexPath, 'utf8')
  const links = [...html.matchAll(/<link\b[^>]*\brel="icon"[^>]*>/g)].map((m) => m[0])
  assert.ok(links.length >= 3, `expected three <link rel="icon"> lines in dist/index.html, found ${links.length}`)
  for (const tag of links) {
    const href = /\bhref="([^"]+)"/.exec(tag)?.[1]
    assert.ok(href && href.startsWith('/assets/'), `icon href must start with /assets/, got ${href} in ${tag}`)
    assert.ok(existsSync(join(dist, href)), `${href} is linked but missing from dist/assets/`)
  }
  const types = links.map((t) => /\btype="([^"]+)"/.exec(t)?.[1])
  assert.deepEqual([...types].sort(), ['image/png', 'image/png', 'image/svg+xml'])
})
