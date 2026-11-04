#!/usr/bin/env bash
# Tests that no agent call in workflows/super-board-wave.js can inherit the session model
# (issue #1, design rule 1: Haiku manages the session, every lane pins its own model).
# No gh calls: the script body runs under node with agent/pipeline/log stubbed. Every
# agent() call must carry a non-empty string opts.model, whatever the tier, whether the
# card was classified or not, and with the pilot on, off, or answering garbage.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }

// calls: every agent() invocation as { label, model }.
// cx: complexity the classify stub returns ('none' = classify answers without one).
// pilot: what the pilot stub answers ('good' | 'garbage' | 'empty' | 'throw' | 'null').
const run = async (args, cx = 'medium', pilot = 'null') => {
  const calls = []
  const agent = async (prompt, opts) => {
    calls.push({ label: opts.label, model: opts.model })
    if (opts.label.startsWith('preflight')) {
      return { verdicts: args.cards.map((c) => ({ number: c.number, verdict: 'proceed', detail: 'clean' })) }
    }
    if (opts.label.startsWith('classify')) {
      return { status: 'ok', kind: 'feature', complexity: cx === 'none' ? undefined : cx }
    }
    if (opts.label.startsWith('pilot')) {
      const n = Number(opts.label.split('#')[1])
      if (pilot === 'throw') throw new Error('pilot boom')
      if (pilot === 'null') return null
      const m = { good: 'sonnet', garbage: 'gpt-5', empty: '   ' }[pilot]
      const lane = { model: m, reason: 'x' }
      return { cards: [{ number: n, lanes: { build: lane, qa: lane, review: lane } }] }
    }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : opts.label.startsWith('build+qa') ? 'Review' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  await new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, () => {})
  return calls
}

const base = { configPath: 'c.json', repo: 'o/r' }
const kindOf = (label) => label.split(':')[0]
const check = (name, calls, want) => {
  const seen = new Set(calls.map((c) => kindOf(c.label)))
  for (const k of want) if (!seen.has(k)) fail(`${name}: no ${k} call was made (saw ${[...seen].join(',')})`)
  for (const c of calls) {
    if (typeof c.model !== 'string' || c.model.trim() === '') {
      fail(`${name}: ${c.label} has model ${JSON.stringify(c.model)}, so it would inherit the session model`)
    }
  }
}

;(async () => {
  const cards = [{ number: 10, status: 'Ready', title: 'a' }]
  const late = [{ number: 31, status: 'QA', title: 'q' }, { number: 32, status: 'Review', title: 'r' }]
  for (const tier of ['low', 'medium', 'high']) {
    const args = (cs, extra = {}) => ({ ...base, tier, cards: cs, ...extra })
    // Classified at each complexity: preflight, classify, build, qa, review all pinned.
    for (const cx of ['low', 'medium', 'high']) {
      check(`${tier}/${cx}`, await run(args(cards), cx), ['preflight', 'classify', 'build', 'qa', 'review'])
    }
    // Classify answers with no complexity: the tier's medium cell, still pinned.
    check(`${tier}/unclassified-ready`, await run(args(cards), 'none'), ['preflight', 'classify', 'build', 'qa', 'review'])
    // Cards entering past Ready are never classified.
    check(`${tier}/late-entry`, await run(args(late)), ['qa', 'review'])
    // Folded build+qa lane (issue #12): pinned too.
    check(`${tier}/fold`, await run(args([{ number: 20, status: 'Ready', title: 'b', labels: ['bug'] }], { foldBuildQa: true })), ['preflight', 'classify', 'build+qa', 'review'])
    // Pilot on: the pilot call is pinned, and a missing, throwing, garbage or empty answer
    // falls back to a ladder cell instead of an empty model.
    for (const p of ['good', 'null', 'throw', 'garbage', 'empty']) {
      for (const cs of [cards, late]) {
        check(`${tier}/pilot-${p}`, await run(args(cs, { pilot: true }), 'medium', p), ['pilot', 'qa', 'review'])
      }
    }
  }

  // An unknown tier must fail loudly, never fall through to an empty (inherited) model.
  let threw = false
  try { await run({ ...base, tier: 'bogus', cards }) } catch (e) { threw = true }
  if (!threw) fail('an unknown tier did not throw')

  console.log('OK: every agent call pins a model')
})().catch((e) => fail(e && e.stack || String(e)))
JS
