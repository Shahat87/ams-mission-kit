#!/usr/bin/env bash
# Tests the opt-in model pilot (Phase 2) in workflows/super-board-wave.js. No gh calls:
# the script body runs under node with agent/pipeline/log stubbed. The stub answers the
# `pilot:#N` label from a scripted map and records every call's opts.model.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }
const eq = (a, b, m) => { if (a !== b) fail(`${m}: expected ${b}, got ${a}`) }
const has = (s, sub, m) => { if (!String(s).includes(sub)) fail(`${m}: "${s}" lacks "${sub}"`) }

// pilotAnswers: card number -> answer object | 'throw' | 'null' (default: pilot returns null).
// classes: card number -> complexity the classify stub returns (default medium).
const run = async (args, pilotAnswers = {}, preflight = 'proceed', classes = {}) => {
  const models = {}, calls = []
  const agent = async (prompt, opts) => {
    calls.push(opts)
    models[opts.label] = opts.model
    if (opts.label.startsWith('pilot')) {
      const a = pilotAnswers[Number(opts.label.split('#')[1])]
      if (a === 'throw') throw new Error('pilot boom')
      if (a === undefined || a === 'null') return null
      return a
    }
    if (opts.label.startsWith('preflight')) {
      return { verdicts: args.cards.map((c) => ({ number: c.number, verdict: preflight, detail: 'clean', files: [], ...(preflight === 'hold' ? { column: 'Blocked' } : {}) })) }
    }
    if (opts.label.startsWith('classify')) return { status: 'ok', kind: 'feature', complexity: classes[Number(opts.label.split('#')[1])] || 'medium' }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  const out = await new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, () => {})
  return { out, models, calls }
}
const base = { configPath: 'c.json', repo: 'o/r' }
const card = (n) => ({ number: n, status: 'Ready', title: 't' })
// Ladder cells for a medium-tier, medium-complexity card: build sonnet, qa sonnet, review sonnet (opus only on a high-complexity card).
const ans = (n, lanes) => ({ cards: [{ number: n, lanes }] })
const L = (model, reason = 'because') => ({ model, reason })
const reasons = (r, n) => Object.fromEntries(r.out.cards.find((c) => c.number === n).modelReasons
  .split(/ (?=(?:build|qa|review)=)/).map((p) => [p.split('=')[0], p.slice(p.indexOf('=') + 1)]))

;(async () => {
  // (a) pilot absent or false: no pilot call, Phase-1 models.
  for (const args of [{ ...base, cards: [card(10)] }, { ...base, pilot: false, cards: [card(10)] }]) {
    const r = await run(args, { 10: ans(10, { build: L('haiku'), qa: L('haiku'), review: L('haiku') }) })
    if (r.calls.some((o) => o.label.startsWith('pilot'))) fail('pilot agent called with pilot off')
    eq(r.models['build:#10'], 'sonnet', 'off: build'); eq(r.models['qa:#10'], 'sonnet', 'off: qa')
    eq(r.models['review:#10'], 'sonnet', 'off: review')
    eq(r.out.cards[0].modelReasons, 'build=ladder medium/medium qa=ladder medium/medium review=ladder medium/medium', 'off: reasons')
  }
  let r = await run({ ...base, tier: 'low', cards: [card(10)] })
  eq(r.models['review:#10'], 'sonnet', 'off: low-tier review')

  // (b)+(h) valid answer: lanes use the pilot's models; reason starts "pilot:"; pilot runs on opus.
  r = await run({ ...base, pilot: true, cards: [card(10)] },
    { 10: ans(10, { build: L('haiku', 'tiny edit'), qa: L('sonnet', 'tests'), review: L('opus', 'merge call') }) })
  eq(r.models['build:#10'], 'haiku', 'pilot build'); eq(r.models['qa:#10'], 'sonnet', 'pilot qa')
  eq(r.models['review:#10'], 'opus', 'pilot review')
  const rs = reasons(r, 10)
  eq(rs.build, 'pilot: tiny edit', 'pilot build reason'); eq(rs.review, 'pilot: merge call', 'pilot review reason')
  const pc = r.calls.filter((o) => o.label.startsWith('pilot'))
  eq(pc.length, 1, 'one pilot call'); eq(pc[0].model, 'opus', 'pilot call model')
  eq(r.out.halted, false, 'halted after valid pilot')

  // (c) opus for build is rejected unless pinned.
  r = await run({ ...base, pilot: true, cards: [card(10)] },
    { 10: ans(10, { build: L('opus'), qa: L('haiku'), review: L('sonnet') }) })
  eq(r.models['build:#10'], 'sonnet', 'opus build falls back')
  has(reasons(r, 10).build, 'pilot rejected', 'opus build reason'); has(reasons(r, 10).build, 'ladder medium/medium', 'opus build ladder reason')
  eq(r.models['qa:#10'], 'haiku', 'other lanes still honoured')

  // (d) fable anywhere, or a non-Claude model, falls back (also case/space-insensitive accepts).
  for (const bad of ['fable', 'gpt-5', '', 'Gemini']) {
    r = await run({ ...base, pilot: true, cards: [card(10)] },
      { 10: ans(10, { build: L(bad), qa: L(bad), review: L(bad) }) })
    eq(r.models['build:#10'], 'sonnet', `${bad} build`); eq(r.models['qa:#10'], 'sonnet', `${bad} qa`)
    eq(r.models['review:#10'], 'sonnet', `${bad} review`)
    has(reasons(r, 10).review, 'pilot rejected', `${bad} reason`)
  }
  r = await run({ ...base, pilot: true, cards: [card(10)] },
    { 10: ans(10, { build: L(' Haiku '), qa: L('SONNET'), review: L('opus') }) })
  eq(r.models['build:#10'], 'haiku', 'normalised model accepted')

  // (e) a pin wins over a valid pilot answer, with reason "pin"; an opus-build pin is honoured.
  r = await run({ ...base, pilot: true, cards: [card(10)], models: { build: 'opus', review: 'fable' } },
    { 10: ans(10, { build: L('haiku'), qa: L('haiku'), review: L('haiku') }) })
  eq(r.models['build:#10'], 'opus', 'pinned build'); eq(r.models['review:#10'], 'fable', 'pinned review')
  eq(r.models['qa:#10'], 'haiku', 'unpinned qa takes pilot')
  eq(reasons(r, 10).build, 'pin', 'pin reason build'); eq(reasons(r, 10).review, 'pin', 'pin reason review')
  has(reasons(r, 10).qa, 'pilot:', 'unpinned lane reason')

  // (f) omitted card or lane falls back to the ladder.
  r = await run({ ...base, pilot: true, cards: [card(10), card(11)] },
    { 10: ans(10, { qa: L('haiku') }), 11: { cards: [{ number: 99, lanes: { build: L('haiku') } }] } })
  eq(r.models['qa:#10'], 'haiku', 'answered lane used')
  eq(r.models['build:#10'], 'sonnet', 'omitted lane: build'); eq(r.models['review:#10'], 'sonnet', 'omitted lane: review')
  has(reasons(r, 10).build, 'pilot rejected', 'omitted lane reason')
  eq(r.models['build:#11'], 'sonnet', 'omitted card: build'); eq(r.models['review:#11'], 'sonnet', 'omitted card: review')
  has(reasons(r, 11).build, 'pilot rejected', 'omitted card reason')

  // (g) pilot throws or returns null / garbage: ladder everywhere, not halted, wave completes.
  for (const a of ['throw', 'null', { nope: true }]) {
    r = await run({ ...base, pilot: true, cards: [card(10)] }, { 10: a })
    eq(r.models['build:#10'], 'sonnet', `${JSON.stringify(a)} build`); eq(r.models['qa:#10'], 'sonnet', `${JSON.stringify(a)} qa`)
    eq(r.models['review:#10'], 'sonnet', `${JSON.stringify(a)} review`)
    eq(r.out.halted, false, `${JSON.stringify(a)} halted`)
    eq(r.out.cards[0].finalStatus, 'advanced', `${JSON.stringify(a)} completes`)
    eq(r.out.cards[0].modelReasons, 'build=ladder medium/medium qa=ladder medium/medium review=ladder medium/medium', `${JSON.stringify(a)} reasons`)
  }

  // cards entering past Ready get a pilot call too, and still obey the rules.
  r = await run({ ...base, pilot: true, cards: [{ number: 31, status: 'QA', title: 't' }] },
    { 31: ans(31, { qa: L('haiku'), review: L('haiku') }) })
  eq(r.models['qa:#31'], 'haiku', 'QA entry qa'); eq(r.models['review:#31'], 'sonnet', 'QA entry review keeps its floor')
  has(reasons(r, 31).review, 'needs a pin', 'review floor reason')

  // case/space tricks: 'Opus ' build and 'FABLE' anywhere are still rejected.
  r = await run({ ...base, pilot: true, cards: [card(10)] },
    { 10: ans(10, { build: L('Opus '), qa: L('FABLE'), review: L(' FaBlE') }) })
  eq(r.models['build:#10'], 'sonnet', 'Opus-space build'); eq(r.models['qa:#10'], 'sonnet', 'FABLE qa')
  eq(r.models['review:#10'], 'sonnet', 'fable review')

  // review floor: the pilot may raise review (sonnet cell -> opus) but never lower it.
  r = await run({ ...base, tier: 'low', pilot: true, cards: [card(10)] }, { 10: ans(10, { review: L('opus') }) })
  eq(r.models['review:#10'], 'opus', 'low-tier review raised')
  r = await run({ ...base, pilot: true, cards: [card(10)] }, { 10: ans(10, { review: L('opus') }) })
  eq(r.models['review:#10'], 'opus', 'medium-card review raised')
  r = await run({ ...base, pilot: true, cards: [card(10)] }, { 10: ans(10, { review: L('haiku') }) })
  eq(r.models['review:#10'], 'sonnet', 'medium-card review not lowered'); has(reasons(r, 10).review, 'needs a pin', 'medium floor reason')
  // a high-complexity card's cell is opus: haiku or sonnet from the pilot is refused.
  for (const low of ['haiku', 'sonnet']) {
    r = await run({ ...base, pilot: true, cards: [card(10)] }, { 10: ans(10, { review: L(low) }) }, 'proceed', { 10: 'high' })
    eq(r.models['review:#10'], 'opus', `high card review not lowered to ${low}`)
    has(reasons(r, 10).review, 'needs a pin', `high floor reason ${low}`)
    has(reasons(r, 10).review, 'ladder medium/high', `high floor ladder reason ${low}`)
  }
  r = await run({ ...base, pilot: true, cards: [card(10)], models: { review: 'haiku' } }, { 10: ans(10, { review: L('opus') }) })
  eq(r.models['review:#10'], 'haiku', 'a pin may lower review')
  r = await run({ ...base, pilot: true, cards: [card(10)], models: { review: { high: 'haiku' } } }, { 10: ans(10, { review: L('opus') }) }, 'proceed', { 10: 'high' })
  eq(r.models['review:#10'], 'haiku', 'a pin may lower a high card review')

  // a card pre-flight holds runs no pilot and no lane, pilot on or off.
  for (const pilot of [true, false]) {
    r = await run({ ...base, pilot, cards: [card(10)] }, { 10: ans(10, { build: L('haiku') }) }, 'hold')
    if (r.calls.some((o) => /^(pilot|build|qa|review):/.test(o.label))) fail(`held card ran a pilot or lane (pilot ${pilot})`)
  }
  // a card in a column with no lane (e.g. Done) pays no pilot call.
  r = await run({ ...base, pilot: true, cards: [{ number: 71, status: 'Done', title: 't' }] }, { 71: ans(71, {}) })
  if (r.calls.some((o) => o.label.startsWith('pilot'))) fail('pilot called for a card with no lane')
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-pilot.sh (off, valid, opus build, fable/gpt, pins, omissions, failure, opus pilot call, review floor, held card)"
