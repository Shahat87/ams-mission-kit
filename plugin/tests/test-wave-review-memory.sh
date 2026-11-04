#!/usr/bin/env bash
# Tests the review-memory wiring in workflows/super-board-wave.js. No gh calls:
# the script body runs under node with agent/pipeline/log stubbed.
#
# The wave script is a workflow body (top-level await + return), not a module,
# so plain `node --check` rejects it. This test is also its syntax check.
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
const base = { configPath: 'c.json', repo: 'o/r' }

;(async () => {
  // 1 — the Review lane is told to load prior_report by its marker; other lanes are not.
  const ok = (label) => label.startsWith('preflight') ? { verdicts: [{ number: 7, verdict: 'proceed', detail: 'clean' }] } : ({ status: 'advanced', column: label.startsWith('review') ? 'Done' : 'next', detail: 'ok',
    ...(label.startsWith('classify') ? { kind: 'feature', complexity: 'low' } : {}) })
  const a = await run({ ...base, cards: [{ number: 7, status: 'Ready', title: 't' }] }, ok)
  const rp = a.prompts['review:#7'] || fail('review lane did not run')
  rp.includes('<!-- super-review:report -->') || fail('review prompt must name the report marker')
  rp.includes('prior_report') || fail('review prompt must ask for prior_report')
  ;/fixed \/ not fixed \/ no longer applies/.test(rp) || fail('review prompt must name the three round-1 verdicts')
  ;/before the builder's PR summary/.test(rp) || fail('review prompt must put the independent pass before the builder summary')
  ;/Gap \/ Bug \/ Verification miss \/ Scope drift \/ Over-engineering/.test(rp) || fail('review prompt must name the finding classes')
  rp.includes('ponytail:ponytail-review') || fail('review prompt must run ponytail-review')
  ;/never blocks merge alone/.test(rp) || fail('review prompt must say Over-engineering never blocks alone')
  ;/fixed brief from super-review/.test(rp) || fail('review prompt must use the fixed truth-check brief')
  ;/reclass any Over-engineering Blocker to Should fix/.test(rp) || fail('review prompt must re-apply the Over-engineering rule after the truth-check')
  ;['build:#7', 'qa:#7'].forEach((l) => a.prompts[l].includes('ponytail-review') && fail(`${l} must not get the review pass`))
  // 1a2 — W42: only the qa lane is told the exact [qa] [report] marker.
  a.prompts['qa:#7'].includes('starts exactly `[qa] [report] ✅`') || fail('qa prompt must name the exact [qa] [report] ✅ marker')
  a.prompts['qa:#7'].includes('`[qa] [report] ❌`') || fail('qa prompt must name the [qa] [report] ❌ marker')
  ;['build:#7', 'review:#7'].forEach((l) => a.prompts[l].includes('starts exactly `[qa] [report] ✅`') && fail(`${l} must not get the qa report marker sentence`))
  ;['build:#7', 'qa:#7'].forEach((l) => a.prompts[l].includes('super-review:report') && fail(`${l} must not get review memory`))
  // 1b — ui-refine-loop is standalone: no lane prompt mentions it.
  ;['build:#7', 'qa:#7', 'review:#7'].forEach((l) => /ui-refine-loop|qa-hook/.test(a.prompts[l]) && fail(`${l} must not trigger ui-refine-loop`))

  // 1c — ams.19: an unlabelled card gets --kind <classified kind> in its lane prompts; a labelled one does not.
  a.prompts['build:#7'].includes('--kind feature') || fail('build prompt must carry --kind <classified kind> for an unlabelled card')
  const lab = await run({ ...base, cards: [{ number: 7, status: 'Ready', title: 't', labels: ['bug'] }] }, ok)
  lab.prompts['build:#7'].includes('has no type label') && fail('a labelled card must not be told to pass --kind')
  // 1d — a docs/ux/tests/refactor/tech-debt/money label is already a type: no --kind note (any case).
  for (const l of ['refactor', 'Docs', 'ux', 'tests', 'tech-debt', 'money']) {
    const t = await run({ ...base, cards: [{ number: 7, status: 'Ready', title: 't', labels: [l] }] }, ok)
    t.prompts['build:#7'].includes('has no type label') && fail(`a ${l}-labelled card must not be told to pass --kind`)
  }
  a.prompts['build:#7'].includes('has no type label') || fail('an unlabelled card must be told about the missing type label')
  // 1e — --model is always passed: the old "omit --model" escape is gone from every lane prompt.
  ;['build:#7', 'qa:#7', 'review:#7'].forEach((l) => {
    /omit --model/.test(a.prompts[l]) && fail(`${l} must not carry the omit --model escape`)
    a.prompts[l].includes('always pass it') || fail(`${l} must say --model is always passed`)
  })

  // 2 — a re-review bounce surfaces round-1 counts in the wave summary.
  const b = await run({ ...base, cards: [{ number: 9, status: 'Review', title: 't' }] },
    () => ({ status: 'bounced', column: 'Ready', detail: '1 prior finding not fixed',
             priorFindings: { fixed: 2, notFixed: 1, noLongerApplies: 0 } }))
  const s = b.out.cards[0]
  s.finalStatus === 'bounced' || fail('expected bounced')
  ;(s.priorFindings && s.priorFindings.notFixed === 1) || fail('summary must carry priorFindings')

  // 3 — a first review (no prior report) reports null, not a fabricated count.
  const c = await run({ ...base, cards: [{ number: 11, status: 'Review', title: 't' }] },
    () => ({ status: 'advanced', column: 'Done', detail: 'merged' }))
  c.out.cards[0].priorFindings === null || fail('first review must report priorFindings: null')

  // 4 — every lane and pre-flight prompt carries the repo slug from args.repo.
  ;['build:#7', 'qa:#7', 'review:#7'].forEach((l) => {
    a.prompts[l].includes('GitHub repository: o/r') || fail(`${l} must carry the repo slug`)
    a.prompts[l].includes('Never derive it from a folder name') || fail(`${l} must forbid deriving the repo from a folder name`)
  })
  const pf = Object.keys(a.prompts).find((l) => l.startsWith('preflight')) || fail('pre-flight did not run')
  a.prompts[pf].includes('GitHub repository: o/r') || fail('pre-flight must carry the repo slug')
  ;(a.prompts['classify:#7'] || fail('classify did not run')).includes('GitHub repository: o/r') || fail('classify must carry the repo slug')

  // 5 — missing or malformed repo is refused.
  const refused = async (args) => { try { await run(args, ok) } catch { return true } return false }
  const cards = [{ number: 7, status: 'Ready', title: 't' }]
  ;(await refused({ configPath: 'c.json', cards })) || fail('args without repo must throw')
  ;(await refused({ configPath: 'c.json', repo: 'AMS-board', cards })) || fail('repo without a slash must throw')
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-review-memory.sh (5 scenarios)"
