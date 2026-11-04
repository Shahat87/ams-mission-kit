#!/usr/bin/env bash
# Tests the lane exit checkpoint (issue #67) in workflows/super-board-wave.js. No gh calls: the script
# body runs under node with agent/pipeline/log stubbed. Build, qa and build+qa prompts carry the
# checkpoint; review does not; the switches (checkpointMax 0, exitCheckpoint false) restore f0fad0c.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const crypto = require('crypto')
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }

const run = async (args) => {
  const calls = []
  const agent = async (prompt, opts) => {
    calls.push({ label: opts.label, model: opts.model, prompt })
    if (opts.label.startsWith('preflight')) {
      return { verdicts: args.cards.map((c) => ({ number: c.number, verdict: 'proceed', detail: 'clean' })) }
    }
    if (opts.label.startsWith('classify')) return { status: 'ok', kind: 'bug', complexity: 'medium' }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  await new AsyncFunction('args', 'agent', 'pipeline', 'log', src + '\n')(args, agent, pipeline, () => {})
  return calls
}

const base = { configPath: 'c.json', repo: 'o/r' }
const cards = [
  { number: 7, status: 'Ready', title: 't', labels: ['bug'] }, { number: 8, status: 'QA', title: 'q', labels: ['feature'] },
  { number: 9, status: 'Ready', title: 'x', labels: ['qa'], lane: 'qa' }, { number: 10, status: 'Review', title: 'r' },
]
const prompt = (calls, label) => { const c = calls.find((x) => x.label === label); if (!c) fail(`no agent call ${label}`); return c.prompt }
const hashes = (calls) => calls.filter((c) => /^(build|qa|review|build\+qa):/.test(c.label))
  .map((c) => c.label + ' ' + crypto.createHash('sha256').update(c.prompt).digest('hex').slice(0, 16)).sort().join('\n')
const CP = 'EXIT CHECKPOINT'

// Prompt hashes at f0fad0c (before the checkpoint existed), taken from the unmodified code.
const REF_PLAIN = ['build:#7 5753463adacbe14a', 'qa:#7 db1bb2d7379379ca', 'qa:#8 0045419e8ceb3e56', 'qa:#9 d33391e57b8a81df',
  'review:#10 036abfa7ef22d597', 'review:#7 b8c69955147cd387', 'review:#8 5495193019fc2620', 'review:#9 4dcaa9c561f3b932'].join('\n')
const REF_FOLD = ['build+qa:#7 773b5f3734f91d85', 'qa:#8 0045419e8ceb3e56', 'qa:#9 d33391e57b8a81df',
  'review:#10 036abfa7ef22d597', 'review:#8 5495193019fc2620', 'review:#9 4dcaa9c561f3b932'].join('\n')

;(async () => {
  // 1. Checkpoint present in build, qa and build+qa with the right prefix and cap 2.
  {
    const plain = await run({ ...base, cards })
    const b = prompt(plain, 'build:#7'), q = prompt(plain, 'qa:#7'), qq = prompt(plain, 'qa:#9')
    for (const [n, p] of [['build', b], ['qa', q], ['qa-card', qq]]) {
      if (!p.includes(CP)) fail(`${n} prompt lacks the checkpoint`)
      if (!p.includes('at most 2 times')) fail(`${n} prompt lacks cap 2`)
      if (p.indexOf(CP) > p.indexOf('Board moves and reads')) fail(`${n}: checkpoint must come before BOARD_IO`)
    }
    if (!b.includes('`[builder]`') || b.includes('`[qa]`')) fail('build checkpoint must name only [builder]')
    if (!q.includes('`[qa]`') || q.includes('`[builder]`')) fail('qa checkpoint must name only [qa]')
    const f = prompt((await run({ ...base, cards, foldBuildQa: true })), 'build+qa:#7')
    if (!f.includes(CP) || !f.includes('at most 2 times')) fail('folded prompt lacks the checkpoint / cap 2')
    if (!f.includes('`[builder]`') || !f.includes('`[qa]`')) fail('folded checkpoint must name both prefixes')
    if (f.indexOf(CP) > f.indexOf('Board moves and reads')) fail('folded: checkpoint must come before BOARD_IO')
    if (f.split(CP).length !== 2) fail('folded prompt must carry the checkpoint exactly once')
  }

  // 2. Review prompts have no checkpoint.
  for (const a of [{}, { foldBuildQa: true }]) {
    for (const c of (await run({ ...base, cards, ...a })).filter((x) => x.label.startsWith('review:'))) {
      if (c.prompt.includes(CP)) fail(`${c.label} prompt carries a checkpoint`)
    }
  }

  // 3. checkpointMax 0 and exitCheckpoint false: byte-identical to f0fad0c.
  for (const off of [{ checkpointMax: 0 }, { exitCheckpoint: false }]) {
    if (hashes(await run({ ...base, cards, ...off })) !== REF_PLAIN) fail(`switch-off ${JSON.stringify(off)} differs from f0fad0c`)
    if (hashes(await run({ ...base, cards, foldBuildQa: true, ...off })) !== REF_FOLD) fail(`switch-off fold ${JSON.stringify(off)} differs from f0fad0c`)
  }
  // Default on differs from f0fad0c only in build, qa(Ready/Building) and build+qa; review hashes are unchanged.
  {
    const got = hashes(await run({ ...base, cards })).split('\n')
    for (const l of REF_PLAIN.split('\n').filter((x) => x.startsWith('review:'))) if (!got.includes(l)) fail(`review hash moved: ${l}`)
    for (const l of REF_PLAIN.split('\n').filter((x) => !x.startsWith('review:'))) if (got.includes(l)) fail(`checkpoint did not change: ${l}`)
  }

  // 4. agent() calls per card are the same with the checkpoint on and off.
  for (const a of [{}, { foldBuildQa: true }]) {
    const on = (await run({ ...base, cards, ...a })).map((c) => c.label).sort()
    const off = (await run({ ...base, cards, ...a, exitCheckpoint: false })).map((c) => c.label).sort()
    if (JSON.stringify(on) !== JSON.stringify(off)) fail(`agent calls differ on/off: ${JSON.stringify(on)} vs ${JSON.stringify(off)}`)
  }

  // 5. checkpointMax is clamped to 3; in-range values pass through; negative clamps to 0 (off); junk falls back to 2.
  const cap = async (v) => {
    const p = prompt(await run({ ...base, cards, checkpointMax: v }), 'build:#7')
    const m = p.match(/at most (\d) times/)
    return m ? Number(m[1]) : null
  }
  if (await cap(9) !== 3) fail('checkpointMax 9 must clamp to 3')
  if (await cap(1) !== 1) fail('checkpointMax 1 must pass through')
  if (await cap(3) !== 3) fail('checkpointMax 3 must pass through')
  if (await cap('x') !== 2) fail('non-numeric checkpointMax must fall back to 2')
  if (await cap(-4) !== null) fail('negative checkpointMax must clamp to 0 (off)')

  console.log('OK: exit checkpoint in build/qa/build+qa only; switches restore f0fad0c; call count unchanged')
})().catch((e) => fail(e && e.stack || String(e)))
JS

echo PASS
