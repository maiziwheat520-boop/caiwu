#!/usr/bin/env node
/**
 * Guard the shared text scale.
 *
 * The 2026-09-04 font-scale pass moved every business text size onto shared
 * tokens with a readable floor, and nothing guarded it afterwards -- a single
 * new rule could quietly put 10px text back into the workbench, and only the
 * user's eyes would catch it. Vitest stubs CSS imports to empty strings, so
 * this reads the stylesheets directly instead of pretending to check them.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')
const MINIMUM_BUSINESS_FONT_PX = 12
const REQUIRED_TOKENS = ['--font-caption', '--font-small', '--font-body']

function stylesheets(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) return stylesheets(path)
    return path.endsWith('.css') ? [path] : []
  })
}

const files = stylesheets(SRC)
const problems = []

if (files.length === 0) problems.push('no stylesheets found under src/')

const root = files.find((file) => file.endsWith(`${join('src', 'styles.css').slice(3)}`) || file.endsWith('styles.css'))
if (!root) {
  problems.push('src/styles.css is missing')
} else {
  const css = readFileSync(root, 'utf8')
  for (const token of REQUIRED_TOKENS) {
    if (!css.includes(`${token}:`)) problems.push(`${token} is no longer defined in src/styles.css`)
  }
}

for (const file of files) {
  const css = readFileSync(file, 'utf8')
  for (const match of css.matchAll(/font-size:\s*([0-9.]+)px/g)) {
    const size = Number(match[1])
    if (size < MINIMUM_BUSINESS_FONT_PX) {
      problems.push(
        `${relative(SRC, file)}: font-size ${size}px is below the ${MINIMUM_BUSINESS_FONT_PX}px floor`,
      )
    }
  }
}

if (problems.length > 0) {
  console.error('typography check failed:')
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exit(1)
}

console.log(`typography check passed: ${files.length} stylesheets at or above ${MINIMUM_BUSINESS_FONT_PX}px`)
