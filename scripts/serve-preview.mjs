#!/usr/bin/env node
/**
 * Serve the built workbench through the real BFF in synthetic mode.
 *
 * Synthetic mode needs no database, no Core and no passkey, and it serves the
 * same synthetic fixtures the offline preview uses -- so the visual baselines
 * contain no financial data.
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const dist = join(root, 'dist')

if (!existsSync(join(dist, 'index.html'))) {
  console.error('dist/ is missing -- run `npm run build` before the visual suite')
  process.exit(1)
}

const python = process.env.LEDGERBRIDGE_PYTHON ?? 'python'
const server = spawn(python, [join(root, 'deploy', 'server.py')], {
  cwd: root,
  stdio: 'inherit',
  env: {
    ...process.env,
    LEDGERBRIDGE_MODE: 'synthetic-preview',
    SITE_ROOT: dist,
    PORT: process.env.PREVIEW_PORT ?? '4173',
    BIND_ADDRESS: '127.0.0.1',
    // Plain HTTP on loopback for the test browser only; production keeps the
    // Secure __Host cookie.
    SESSION_COOKIE_SECURE: '0',
  },
})

const stop = () => {
  if (!server.killed) server.kill()
}
process.on('SIGINT', stop)
process.on('SIGTERM', stop)
process.on('exit', stop)

server.on('exit', (code) => process.exit(code ?? 0))
