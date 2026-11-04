#!/usr/bin/env bash
# Tests the per-lane model ladder and pins in workflows/super-board-wave.js. No gh calls:
# the script body runs under node with agent/pipeline/log stubbed, recording opts.model.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }
const eq = (a, b, m) => { if (a !== b) fail(`${m}: expected ${b}, got ${a}`) }

// classes: map card number -> complexity (cards at Ready get classified).
const run = async (args, classes = {}) => {
  const models = {}
  const agent = async (prompt, opts) => {
    models[opts.label] = opts.model
    if (opts.label.startsWith('preflight')) {
      return { verdicts: args.cards.map((c) => ({ number: c.number, verdict: 'proceed', detail: 'clean', files: [] })) }
    }
    if (opts.label.startsWith('classify')) {
      const cx = classes[Number(opts.label.split('#')[1])]
      return { status: 'ok', kind: 'feature', complexity: cx === 'none' ? undefined : (cx || 'medium') }
    }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  const out = await new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, () => {})
  return { out, models }
}
const base = { configPath: 'c.json', repo: 'o/r' }
const TABLE = {
  low: { build: ['haiku', 'sonnet', 'sonnet'], qa: ['haiku', 'haiku', 'sonnet'], review: ['sonnet', 'sonnet', 'sonnet'] },
  medium: { build: ['sonnet', 'sonnet', 'sonnet'], qa: ['sonnet', 'sonnet', 'sonnet'], review: ['sonnet', 'sonnet', 'opus'] },
  high: { build: ['sonnet', 'sonnet', 'sonnet'], qa: ['sonnet', 'sonnet', 'sonnet'], review: ['sonnet', 'sonnet', 'opus'] },
}
const CX = ['low', 'medium', 'high']
const cardsFor = () => CX.map((c, i) => ({ number: 10 + i, status: 'Ready', title: c }))
const classesFor = () => Object.fromEntries(CX.map((c, i) => [10 + i, c]))

;(async () => {
  // (a)(b)(c) every tier x complexity matches the table; no build lane is opus/fable.
  for (const tier of ['low', 'medium', 'high']) {
    const args = tier === 'medium' ? { ...base, cards: cardsFor() } : { ...base, tier, cards: cardsFor() }
    const { models } = await run(args, classesFor())
    CX.forEach((c, i) => {
      const n = 10 + i
      eq(models[`build:#${n}`], TABLE[tier].build[i], `${tier}/${c} build`)
      eq(models[`qa:#${n}`], TABLE[tier].qa[i], `${tier}/${c} qa`)
      eq(models[`review:#${n}`], TABLE[tier].review[i], `${tier}/${c} review`)
      if (['opus', 'fable'].includes(models[`build:#${n}`])) fail(`${tier}/${c} builds on ${models[`build:#${n}`]}`)
    })
  }

  // (d) cards entering past Ready are never classified: tier's medium cell.
  for (const tier of ['low', 'medium', 'high']) {
    const { models } = await run({ ...base, tier, cards: [
      { number: 31, status: 'QA', title: 't' }, { number: 32, status: 'Review', title: 't' }] })
    eq(models['qa:#31'], TABLE[tier].qa[1], `${tier} QA entry qa`)
    eq(models['review:#31'], TABLE[tier].review[1], `${tier} QA entry review`)
    eq(models['review:#32'], TABLE[tier].review[1], `${tier} Review entry review`)
    if ('build:#31' in models || 'qa:#32' in models) fail('lane ran that should not have')
  }

  // (e) a pin wins; a complexity-table pin touches only that complexity.
  let r = await run({ ...base, cards: cardsFor(), models: { build: 'opus' } }, classesFor())
  CX.forEach((c, i) => eq(r.models[`build:#${10 + i}`], 'opus', `pin build ${c}`))
  eq(r.models['qa:#12'], 'sonnet', 'unpinned qa unchanged')
  r = await run({ ...base, cards: cardsFor(), models: { qa: { high: 'fable' } } }, classesFor())
  eq(r.models['qa:#12'], 'fable', 'high qa pinned to fable')
  eq(r.models['qa:#11'], 'sonnet', 'medium qa stays on ladder')
  eq(r.models['qa:#10'], 'sonnet', 'low-complexity qa takes the medium-tier cell')
  r = await run({ ...base, cards: [{ number: 41, status: 'QA', title: 't' }], models: { review: { medium: 'haiku' } } })
  eq(r.models['review:#41'], 'haiku', 'unclassified card takes the medium pin cell')

  // (f) bad input throws at wave start.
  for (const [label, models] of [
    ['unknown model string', { build: 'gpt-5' }],
    ['unknown model in table', { qa: { high: 'gemini' } }],
    ['unknown lane', { deploy: 'sonnet' }],
    ['unknown complexity', { build: { huge: 'sonnet' } }],
    ['non-object', 'sonnet'],
    ['empty array', []],
    ['array', ['sonnet']],
  ]) {
    let threw = false
    try { await run({ ...base, cards: cardsFor(), models }, classesFor()) } catch (e) { threw = /models pin/.test(e.message) }
    if (!threw) fail(`${label} did not throw a models-pin error`)
  }

  // (g) recorded choice: history entries and summary carry model + modelReason.
  r = await run({ ...base, cards: cardsFor(), models: { review: 'fable' } }, classesFor())
  const c = r.out.cards.find((x) => x.number === 12)
  eq(c.models, 'build=sonnet qa=sonnet review=fable', 'summary models')
  eq(c.modelReasons, 'build=ladder medium/high qa=ladder medium/high review=pin', 'summary modelReasons')
  r = await run({ ...base, cards: [{ number: 51, status: 'QA', title: 't' }] })
  eq(r.out.cards[0].modelReasons, 'qa=ladder medium/unclassified review=ladder medium/unclassified', 'unclassified reasons')

  // a classify result with no complexity takes the medium cell; models: null means no pins.
  r = await run({ ...base, cards: [{ number: 61, status: 'Ready', title: 't' }], models: null }, { 61: 'none' })
  eq(r.models['build:#61'], 'sonnet', 'missing complexity build')
  eq(r.models['review:#61'], 'sonnet', 'missing complexity review')
  eq(r.out.cards[0].modelReasons, 'build=ladder medium/unclassified qa=ladder medium/unclassified review=ladder medium/unclassified', 'missing complexity reasons')
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-model-ladder.sh (table, no-opus build, unclassified, pins, bad input, recorded choice)"
