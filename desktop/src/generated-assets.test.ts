import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const assets = fileURLToPath(new URL('../ui/', import.meta.url))
const repo = resolve(assets, '../..')
const scripts = readdirSync(assets).filter(name => name.endsWith('.js'))

function referencedAssets() {
  const result = new Set<string>()
  for (const script of scripts) {
    const text = readFileSync(resolve(assets, script), 'utf8')
    for (const match of text.matchAll(/(?:from\s*|import\s*)['"](\.\/[^'"]+)['"]/g)) {
      result.add(resolve(assets, match[1]))
    }
    for (const match of text.matchAll(/sourceMappingURL=([^\s]+)/g)) {
      result.add(resolve(assets, match[1]))
    }
  }
  return [...result]
}

describe('checked-in native loader assets', () => {
  it('has every relative module and source map used by generated scripts', () => {
    expect(scripts).toContain('main.js')
    expect(referencedAssets().length).toBeGreaterThan(0)
    for (const asset of referencedAssets()) {
      expect(existsSync(asset), `Missing generated asset: ${relative(repo, asset)}`).toBe(true)
    }
  })

  it.skipIf(!existsSync(resolve(repo, '.git')))('does not rely on untracked generated modules', () => {
    const tracked = new Set(execFileSync('git', ['ls-files', '--', 'desktop/ui'], {
      cwd: repo, encoding: 'utf8',
    }).trim().split('\n'))
    for (const asset of referencedAssets()) {
      expect(tracked.has(relative(repo, asset)), `Untracked generated asset: ${relative(repo, asset)}`).toBe(true)
    }
  })
})
