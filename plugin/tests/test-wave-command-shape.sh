#!/usr/bin/env bash
# Tests that every lane prompt in workflows/super-board-wave.js tells the lane to run
# GitHub/git writes as standalone Bash calls. No gh calls: the script body runs under
# node with agent/pipeline/log stubbed.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const run = (args, reply) => {
  const prompts = {}
  const agent = async (prompt, opts) => { prompts[opts.label] = prompt; return reply(opts.label) }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  return new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, () => {})
    .then((out) => ({ out, prompts }))
}
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }
const ok = (label) => label.startsWith('preflight') ? { verdicts: [{ number: 7, verdict: 'proceed', detail: 'clean' }] } : ({ status: 'advanced', column: label.startsWith('review') ? 'Done' : 'next', detail: 'ok',
  ...(label.startsWith('classify') ? { kind: 'feature', complexity: 'low' } : {}) })

;(async () => {
  const a = await run({ configPath: 'c.json', repo: 'o/r', cards: [{ number: 7, status: 'Ready', title: 't' }] }, ok)
  for (const l of ['build:#7', 'qa:#7', 'review:#7']) {
    const p = a.prompts[l] || fail(`${l} lane did not run`)
    p.includes('as its own Bash call') || fail(`${l} prompt must say writes run as their own Bash call`)
    p.includes('git -C') || fail(`${l} prompt must say to use git -C instead of cd`)
    // Parallel lanes share one session scratchpad: a fixed status.md let card #221's
    // QA lane overwrite card #224's before it was posted (golden eval 20261031T005927Z).
    p.includes('card-7/') || fail(`${l} prompt must name a per-card scratch folder (card-7/)`)
  }
  const pf = Object.keys(a.prompts).find((k) => k.startsWith('preflight')) || fail('preflight did not run')
  a.prompts[pf].includes('as its own Bash call') || fail('preflight prompt must say writes run as their own Bash call')
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-command-shape.sh (preflight, build, qa, review; per-card scratch)"
