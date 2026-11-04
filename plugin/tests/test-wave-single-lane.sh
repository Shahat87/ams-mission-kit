#!/usr/bin/env bash
# Tests the singleLane arg (issue #66) in workflows/super-board-wave.js. No gh calls: the script
# body runs under node with agent/pipeline/log stubbed. singleLane:true must stop after the first
# runLane of a card; without the arg the chaining must be byte-identical to today.
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
      return { verdicts: args.cards.map((c) => ({ number: c.number, verdict: 'proceed', detail: 'clean', files: [] })) }
    }
    if (opts.label.startsWith('classify')) return { status: 'ok', kind: 'bug', complexity: 'medium' }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  const out = await new AsyncFunction('args', 'agent', 'pipeline', 'log', src + '\n')(args, agent, pipeline, () => {})
  return { calls, out }
}

const lanes = (calls) => calls.filter((c) => !/^(preflight|classify)/.test(c.label)).map((c) => c.label.split(':')[0])
const same = (name, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) fail(`${name}: lanes ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`)
}
const hashOf = (call) => crypto.createHash('sha256').update(call.prompt).digest('hex').slice(0, 16)
const base = { configPath: 'c.json', repo: 'o/r' }
const bug = [{ number: 10, status: 'Ready', title: 'b', labels: ['bug'] }]
const feature = [{ number: 11, status: 'Ready', title: 'f', labels: ['feature'] }]
const qaLabel = [{ number: 12, status: 'Ready', title: 'q', labels: ['qa'], lane: 'qa' }]
const qaStatus = [{ number: 13, status: 'QA', title: 'q2', labels: [] }]
const reviewStatus = [{ number: 14, status: 'Review', title: 'r', labels: [] }]

;(async () => {
  // Without singleLane (absent, false, null): the chain is unchanged.
  for (const extra of [{}, { singleLane: false }, { singleLane: null }]) {
    same(`off/bug ${JSON.stringify(extra)}`, lanes((await run({ ...base, cards: bug, ...extra })).calls), ['build', 'qa', 'review'])
    same(`off/qa ${JSON.stringify(extra)}`, lanes((await run({ ...base, cards: qaLabel, ...extra })).calls), ['qa', 'review'])
  }

  // With singleLane:true exactly one lane runs per card — the entry-point lane.
  same('on/bug', lanes((await run({ ...base, cards: bug, singleLane: true })).calls), ['build'])
  same('on/feature', lanes((await run({ ...base, cards: feature, singleLane: true })).calls), ['build'])
  same('on/qa-label', lanes((await run({ ...base, cards: qaLabel, singleLane: true })).calls), ['qa'])
  same('on/qa-status', lanes((await run({ ...base, cards: qaStatus, singleLane: true })).calls), ['qa'])
  same('on/review', lanes((await run({ ...base, cards: reviewStatus, singleLane: true })).calls), ['review'])

  // The fold is one lane; singleLane stops after it.
  same('on/bug+fold', lanes((await run({ ...base, cards: bug, singleLane: true, foldBuildQa: true })).calls), ['build+qa'])

  // The single lane's prompt is byte-identical with and without singleLane.
  {
    const off = await run({ ...base, cards: [...bug, ...qaLabel, ...qaStatus, ...reviewStatus] })
    const on = await run({ ...base, cards: [...bug, ...qaLabel, ...qaStatus, ...reviewStatus], singleLane: true })
    for (const [name, card] of [['build', bug[0]], ['qa', qaLabel[0]], ['qa', qaStatus[0]], ['review', reviewStatus[0]]]) {
      const want = off.calls.find((c) => c.label === `${name}:#${card.number}`)
      const got = on.calls.find((c) => c.label === `${name}:#${card.number}`)
      if (!got) fail(`singleLane lost the ${name}:#${card.number} lane entirely`)
      if (hashOf(got) !== hashOf(want)) fail(`singleLane changed the ${name}:#${card.number} prompt (${hashOf(want)} -> ${hashOf(got)})`)
    }
  }

  // singleLane:true stops even after a non-advanced result; nothing else runs.
  {
    const calls = []
    const agent = async (prompt, opts) => {
      calls.push({ label: opts.label })
      if (opts.label.startsWith('preflight')) {
        return { verdicts: [{ number: 10, verdict: 'proceed', detail: 'clean', files: [] }] }
      }
      return { status: 'failed', column: 'QA', detail: 'boom' }
    }
    const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
      let v = it
      for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
      return v
    }))
    const out = await new AsyncFunction('args', 'agent', 'pipeline', 'log', src + '\n')(
      { ...base, cards: bug, singleLane: true }, agent, pipeline, () => {})
    same('on/failed-build', lanes(calls), ['build'])
    if (!out.cards[0].lanesRun.startsWith('build:failed')) fail(`lanesRun ${out.cards[0].lanesRun}`)
  }

  // Junk args fail loudly; booleans pass validation.
  for (const junk of ['yes', 1, 'true']) {
    try {
      await run({ ...base, cards: bug, singleLane: junk })
      fail(`singleLane ${JSON.stringify(junk)} must throw`)
    } catch (e) {
      if (!/singleLane/.test(String(e && e.message))) fail(`singleLane ${JSON.stringify(junk)} threw something else: ${e}`)
    }
  }

  console.log('OK: singleLane stops after the first lane; off chaining and prompts are unchanged')
})().catch((e) => fail(e && e.stack || String(e)))
JS

# The docs carry the switch: run-workflow.md documents singleLane, events.md exists,
# and the config schema has the events block.
RUNWF=../skills/super-board/references/run-workflow.md
EVENTS=../skills/super-board/references/events.md
SCHEMA=../skills/super-board/references/config-schema.json
grep -q 'singleLane' "$RUNWF" || { echo "FAIL: run-workflow.md lost the singleLane doc"; exit 1; }
[ -f "$EVENTS" ] || { echo "FAIL: references/events.md missing"; exit 1; }
grep -q '"events"' "$SCHEMA" || { echo "FAIL: config-schema.json lost the events block"; exit 1; }
