#!/usr/bin/env bash
# Tests the Builder pre-flight wiring in workflows/super-board-wave.js. No gh
# calls: the script body runs under node with agent/pipeline/log stubbed, and the
# pre-flight agent's verdicts are scripted per card.
#
# The rule: no Ready card reaches runLane('build') without a pre-flight verdict
# that says go. A hold, a sequence-behind, or a missing verdict ends the card's
# chain for this wave and shows up in the summary — never silently dropped.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const run = (args, verdicts) => {
  const labels = [], prompts = {}
  const agent = async (prompt, opts) => {
    labels.push(opts.label); prompts[opts.label] = prompt
    if (opts.label.startsWith('preflight')) {
      const nums = opts.label.slice('preflight:'.length).split(',').map((s) => Number(s.slice(1)))
      // Scripted verdicts echo files: [] unless the scenario sets files (files: null = the agent dropped it).
      return { verdicts: nums.filter((n) => verdicts[n]).map((n) => {
        const v = { number: n, files: [], ...verdicts[n] }
        if (v.files === null) delete v.files
        return v }) }
    }
    if (opts.label.startsWith('classify')) return { kind: 'feature', complexity: 'low' }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  return new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, () => {})
    .then((out) => ({ out, labels, prompts }))
}
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }
const card = (number, status = 'Ready', extra = {}) => ({ number, status, title: `t${number}`, ...extra })
const by = (out, n) => out.cards.find((c) => c.number === n)

;(async () => {
  const { out, labels } = await run({ configPath: 'c.json', repo: 'o/r',
    cards: [card(1), card(2), card(3), card(4), card(5), card(6, 'Review')] }, {
      1: { verdict: 'proceed', detail: 'clean' },
      2: { verdict: 'hold', column: 'Blocked', detail: 'duplicate of merged PR #31' },
      3: { verdict: 'sequence', column: 'Blocked', detail: 'behind #50 (PR #41 touches login.ts)' },
      4: { verdict: 'sequence', column: 'Ready', detail: 'expected conflict with PR #44 — merge gate rebases' },
      // 5: no verdict at all
    })

  // 1 — proceed builds.
  labels.includes('build:#1') || fail('#1 proceed must build')
  // 2 — hold never builds, and is reported as a pre-flight block in Blocked.
  labels.includes('build:#2') && fail('#2 hold must not build')
  ;(by(out, 2).lastLane === 'preflight' && by(out, 2).finalStatus === 'blocked' && by(out, 2).column === 'Blocked')
    || fail('#2 must be reported preflight:blocked in Blocked: ' + JSON.stringify(by(out, 2)))
  ;/#31/.test(by(out, 2).detail) || fail('#2 detail must name the duplicate')
  // 3 — sequence behind an issue parks the card; 4 — sequence with only a conflict note builds.
  labels.includes('build:#3') && fail('#3 sequenced behind #50 must not build this wave')
  labels.includes('build:#4') || fail('#4 conflict-note sequence must build')
  // 5 — a missing verdict is never built unchecked; it stays Ready for the next wave.
  labels.includes('build:#5') && fail('#5 had no verdict and must not build')
  ;(by(out, 5).finalStatus === 'failed' && by(out, 5).column === 'Ready') || fail('#5 must be failed@Ready: ' + JSON.stringify(by(out, 5)))
  // 6 — cards already past Ready are not pre-flighted.
  labels.some((l) => l.startsWith('preflight') && /#6\b/.test(l)) && fail('#6 in Review must not be pre-flighted')
  labels.includes('review:#6') || fail('#6 review must still run')

  // 7 — batching: 5 Ready cards fit one pre-flight agent.
  labels.filter((l) => l.startsWith('preflight')).length === 1 || fail('5 Ready cards = 1 pre-flight agent, got ' + labels)
  const seven = await run({ configPath: 'c.json', repo: 'o/r', cards: [1, 2, 3, 4, 5, 6, 7].map((n) => card(n)) },
    Object.fromEntries([1, 2, 3, 4, 5, 6, 7].map((n) => [n, { verdict: 'proceed', detail: 'clean' }])))
  seven.labels.filter((l) => l.startsWith('preflight')).length === 2 || fail('7 Ready cards = 2 pre-flight agents')

  // 8 — a qa-labelled Ready card skips Building: no pre-flight, no Builder, Tester first,
  //     told to move the card Ready → QA itself. Lane from the planner or from labels.
  const qa = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1, 'Ready', { lane: 'qa', labels: ['qa'] }), card(2, 'Ready', { labels: ['QA'] })] }, {})
  qa.labels.some((l) => l.startsWith('preflight')) && fail('qa cards must not pre-flight')
  qa.labels.some((l) => l.startsWith('build')) && fail('qa cards must not build')
  ;(qa.labels.includes('qa:#1') && qa.labels.includes('qa:#2')) || fail('qa Ready cards go to QA: ' + qa.labels)
  ;/Ready → QA/.test(qa.prompts['qa:#1']) || fail('the Tester must be told to move a qa card Ready → QA')

  // 9 — bug, feature and no label all build; the classifier keeps a label and labels a bare card.
  const mix = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(3, 'Ready', { lane: 'build', labels: ['bug'] }), card(4)] },
    { 3: { verdict: 'proceed', detail: 'ok' }, 4: { verdict: 'proceed', detail: 'ok' } })
  ;(mix.labels.includes('build:#3') && mix.labels.includes('build:#4')) || fail('bug and unlabelled cards build: ' + mix.labels)
  ;/label says "bug"/.test(mix.prompts['classify:#3']) || fail('classifier must keep the bug label')
  ;(/--add-label/.test(mix.prompts['classify:#4']) && /never qa/.test(mix.prompts['classify:#4']))
    || fail('classifier must label a bare card feature or bug, never qa')

  // 11 — ams.19: the pre-flight prompt's move command carries --lane build and --model (its move to Blocked
  //      would be refused without them); an unlabelled card gets the --kind note, a refactor card does not.
  const pf = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2, 'Ready', { labels: ['refactor'] })] },
    { 1: { verdict: 'proceed', detail: 'ok' }, 2: { verdict: 'proceed', detail: 'ok' } })
  const pfp = pf.prompts['preflight:#1,#2'] || fail('pre-flight did not run: ' + pf.labels)
  pfp.includes("super-board-preflight.sh --config 'c.json'") || fail('pre-flight prompt must run the script with --config <configPath> (W28: ancestry check needs base_branch)')
  pfp.includes('--lane build') || fail('pre-flight prompt must carry --lane build')
  pfp.includes('--model <your own model family name') || fail('pre-flight prompt must carry --model')
  ;/omit --model/.test(pfp) && fail('pre-flight prompt must not carry the omit --model escape')
  pfp.includes('Card #1 has no type label') && /--kind/.test(pfp) || fail('pre-flight must give --kind for the unlabelled card')
  pfp.includes('Card #2 has no type label') && fail('a refactor-labelled card needs no --kind')
  // 12 — one --files list on a batch made false peer overlaps (2026-10-30): the batch runs without it.
  ;/without --files/.test(pfp) || fail('pre-flight prompt must say a batch runs without --files')

  // 13 — W23 deterministic peer overlap: two building cards sharing a file never build in one wave.
  //      The higher number is deferred (skipped -> stays Ready, never Blocked); the lower builds.
  const ov = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2), card(3)] }, {
    1: { verdict: 'proceed', detail: 'clean', files: ['kwd.py', 'tests/test_kwd.py'] },
    2: { verdict: 'proceed', detail: 'different function', files: ['kwd.py'] },
    3: { verdict: 'proceed', detail: 'clean', files: ['other.py'] },
  })
  ov.labels.includes('build:#1') || fail('(d) lower card #1 must build')
  ov.labels.includes('build:#2') && fail('(d) #2 shares kwd.py with #1 and must not build this wave')
  ;(by(ov.out, 2).finalStatus === 'failed' && by(ov.out, 2).column === 'Ready' && by(ov.out, 2).lastLane === 'preflight')
    || fail('(d) #2 must be preflight failed@Ready, never Blocked: ' + JSON.stringify(by(ov.out, 2)))
  ;/preflight:deferred behind #1 \(shared kwd\.py\)/.test(by(ov.out, 2).detail) || fail('(d) detail: ' + by(ov.out, 2).detail)
  // (e) no shared file: both build.
  ov.labels.includes('build:#3') || fail('(e) #3 shares no file and must build')
  // (e) two proceed verdicts with disjoint files both build.
  const dj = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2)] }, {
    1: { verdict: 'proceed', detail: 'ok', files: ['a.py'] }, 2: { verdict: 'proceed', detail: 'ok', files: ['b.py'] } })
  ;(dj.labels.includes('build:#1') && dj.labels.includes('build:#2')) || fail('(e) disjoint files must both build: ' + dj.labels)
  // (f) an EMPTY files array is never deferred, even beside a card that has files (the script found no paths).
  const nf = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2)] }, {
    1: { verdict: 'proceed', detail: 'ok', files: ['a.py'] }, 2: { verdict: 'proceed', detail: 'ok', files: [] } })
  ;(nf.labels.includes('build:#1') && nf.labels.includes('build:#2'))
    || fail('(f) a card with an empty files array must build: ' + nf.labels)
  // (g) W34 fail closed: a building card whose verdict echoes NO files array, with a lower card held, is deferred
  //     (skipped @ Ready) - overlap is unknown. Never Blocked.
  const mf = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2)] }, {
    1: { verdict: 'proceed', detail: 'ok', files: ['a.py'] }, 2: { verdict: 'proceed', detail: 'ok', files: null } })
  mf.labels.includes('build:#1') || fail('(g) #1 must build')
  mf.labels.includes('build:#2') && fail('(g) #2 echoed no files beside held #1 and must not build')
  ;(by(mf.out, 2).finalStatus === 'failed' && by(mf.out, 2).column === 'Ready' && by(mf.out, 2).lastLane === 'preflight')
    || fail('(g) #2 must be preflight failed@Ready: ' + JSON.stringify(by(mf.out, 2)))
  ;/preflight:deferred \(no files echoed; overlap unknown\)/.test(by(mf.out, 2).detail) || fail('(g) detail: ' + by(mf.out, 2).detail)
  // (h) the lowest building card with no files array builds: no lower card is held.
  const lo = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2)] }, {
    1: { verdict: 'proceed', detail: 'ok', files: null }, 2: { verdict: 'proceed', detail: 'ok', files: ['b.py'] } })
  lo.labels.includes('build:#1') || fail('(h) lowest card with files missing must build: ' + lo.labels)
  // (k) W34 review: the lowest card with files missing builds, but its overlap is unknown, so every later building
  //     card with files defers behind it (skipped @ Ready). An empty files array is never deferred.
  const uk = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2), card(3)] }, {
    1: { verdict: 'proceed', detail: 'ok', files: null }, 2: { verdict: 'proceed', detail: 'ok', files: ['a.py'] },
    3: { verdict: 'proceed', detail: 'ok', files: [] } })
  uk.labels.includes('build:#1') || fail('(k) #1 (files missing, lowest) must build')
  uk.labels.includes('build:#2') && fail('(k) #2 must not build beside #1 whose overlap is unknown')
  ;(by(uk.out, 2).finalStatus === 'failed' && by(uk.out, 2).column === 'Ready' && by(uk.out, 2).lastLane === 'preflight')
    || fail('(k) #2 must be preflight failed@Ready: ' + JSON.stringify(by(uk.out, 2)))
  ;/preflight:deferred behind #1 \(overlap unknown: #1 echoed no files\)/.test(by(uk.out, 2).detail) || fail('(k) detail: ' + by(uk.out, 2).detail)
  uk.labels.includes('build:#3') || fail('(k) an empty files array is never deferred')
  // (i) the schema makes files required, and the prompt says to always include it.
  ;/required: \['number', 'verdict', 'detail', 'files'\]/.test(src) || fail('(i) PREFLIGHT_SCHEMA must require files')
  ;/Always include "files"/.test(ov.prompts['preflight:#1,#2,#3']) || fail('(i) prompt must say to always include files')
  // A deferred card is not building, so it does not hold back a higher card; a conflict-note sequence counts as building.
  const ch = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2), card(3), card(4)] }, {
    1: { verdict: 'proceed', detail: 'ok', files: ['a.py'] },
    2: { verdict: 'proceed', detail: 'ok', files: ['a.py', 'b.py'] },
    3: { verdict: 'proceed', detail: 'ok', files: ['b.py'] },
    4: { verdict: 'sequence', column: 'Ready', detail: 'conflict note', files: ['b.py'] } })
  ;(ch.labels.includes('build:#1') && !ch.labels.includes('build:#2') && ch.labels.includes('build:#3') && !ch.labels.includes('build:#4'))
    || fail('deferred cards must not block higher cards; Ready-sequence builds and defers like proceed: ' + ch.labels)
  ;/Copy each card's "files"/.test(ov.prompts['preflight:#1,#2,#3']) || fail('pre-flight prompt must tell the agent to copy files')

  // (j) W34 path rule: equal ignoring case, or a bare name equal to the other's last segment.
  const meet = async (fa, fb) => {
    const r = await run({ configPath: 'c.json', repo: 'o/r', cards: [card(1), card(2)] }, {
      1: { verdict: 'proceed', detail: 'ok', files: fa }, 2: { verdict: 'proceed', detail: 'ok', files: fb } })
    return !r.labels.includes('build:#2')
  }
  ;(await meet(['kwd.py'], ['src/kwd.py'])) || fail('(j) kwd.py vs src/kwd.py must meet')
  ;(await meet(['src/kwd.py'], ['kwd.py'])) || fail('(j) src/kwd.py vs kwd.py must meet')
  ;(await meet(['KWD.py'], ['kwd.py'])) || fail('(j) KWD.py vs kwd.py must meet')
  ;(await meet(['tests/test_kwd.py'], ['test_kwd.py'])) || fail('(j) tests/test_kwd.py vs test_kwd.py must meet')
  ;(await meet(['src/a.py'], ['lib/a.py'])) && fail('(j) src/a.py vs lib/a.py must NOT meet (both have a slash)')

  // 10 — a stale caller passing the removed qa-only variant is refused, not guessed.
  let threw = false
  try { await run({ configPath: 'c.json', repo: 'o/r', variant: 'qa-only', cards: [card(1)] }, {}) } catch { threw = true }
  threw || fail('variant qa-only must be refused')
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-preflight.sh (17 scenarios)"
