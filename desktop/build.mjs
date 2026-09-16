#!/usr/bin/env node
// Copies the shell's static assets (HTML/CSS) from src/ to ui/ — the
// directory `src-tauri/tauri.conf.json`'s `build.frontendDist` serves.
// TypeScript compiles everything else (see package.json's "build" script,
// which runs `tsc -p tsconfig.build.json` first); this script only moves
// the two files tsc does not touch, so `ui/` stays a complete, committed
// build output even if a CI step never runs this package's `npm run build`.
import { copyFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const srcDir = join(here, 'src')
const outDir = join(here, 'ui')

mkdirSync(outDir, { recursive: true })
for (const asset of ['index.html', 'styles.css']) {
  copyFileSync(join(srcDir, asset), join(outDir, asset))
}
