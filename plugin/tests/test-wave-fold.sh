#!/usr/bin/env bash
# Tests the opt-in build+qa fold (issue #12) in workflows/super-board-wave.js. No gh calls: the script
# body runs under node with agent/pipeline/log stubbed. Off (default) must equal today's lanes.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const crypto = require('crypto')
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }

// foldResult: what the build+qa stub answers. Other lanes always advance.
const run = async (args, foldResult = { status: 'advanced', column: 'Review', detail: 'ok' }) => {
  const calls = []
  let out
  const agent = async (prompt, opts) => {
    calls.push({ label: opts.label, model: opts.model, prompt })
    if (opts.label.startsWith('preflight')) {
      return { verdicts: args.cards.map((c) => ({ number: c.number, verdict: 'proceed', detail: 'clean' })) }
    }
    if (opts.label.startsWith('classify')) return { status: 'ok', kind: 'bug', complexity: 'medium' }
    if (opts.label.startsWith('build+qa')) return foldResult
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  out = await new AsyncFunction('args', 'agent', 'pipeline', 'log', src + '\n')(args, agent, pipeline, () => {})
  return { calls, out }
}

const lanes = (calls) => calls.filter((c) => !/^(preflight|classify)/.test(c.label)).map((c) => c.label.split(':')[0])
const same = (name, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) fail(`${name}: lanes ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`)
}
const base = { configPath: 'c.json', repo: 'o/r' }
const bug = [{ number: 10, status: 'Ready', title: 'b', labels: ['bug'] }]
const feature = [{ number: 11, status: 'Ready', title: 'f', labels: ['feature'] }]
const qa = [{ number: 12, status: 'Ready', title: 'q', labels: ['qa'], lane: 'qa' }]

;(async () => {
  // Off (key absent, and explicit false): build, qa, review as separate agents.
  for (const extra of [{}, { foldBuildQa: false }]) {
    const { calls } = await run({ ...base, cards: bug, ...extra })
    same('off/bug', lanes(calls), ['build', 'qa', 'review'])
  }

  // On: a bug card runs build+qa then a separate review agent.
  {
    const { calls, out } = await run({ ...base, cards: bug, foldBuildQa: true })
    same('on/bug', lanes(calls), ['build+qa', 'review'])
    const c = out.cards[0]
    if (!c.lanesRun.startsWith('build+qa:advanced → review:')) fail(`lanesRun missing build+qa: ${c.lanesRun}`)
    if (!/build\+qa=\w+/.test(c.models)) fail(`models missing build+qa: ${c.models}`)
    // The build+qa agent gets a non-empty model; so does review.
    for (const x of calls) if (typeof x.model !== 'string' || !x.model.trim()) fail(`${x.label} has no model`)
    // Prompt = Builder then Tester, one task.
    const p = calls.find((x) => x.label.startsWith('build+qa')).prompt
    if (!(p.indexOf('super-build') > 0 && p.indexOf('super-build') < p.indexOf('super-qa'))) fail('folded prompt must run the Builder before the Tester')
    // Taint check comes before any build instruction.
    const t = p.indexOf('Tainted cards')
    if (t < 0 || t > p.indexOf('super-build')) fail('folded prompt: taint check must precede any build instruction')
    if (!p.includes('tainted, rerun unfolded')) fail('folded prompt lost the tainted bounce wording')
    if (!p.includes('[qa] [report] ✅')) fail('folded prompt must name the exact [qa] [report] ✅ marker')
    if (!p.includes('[qa] [report] ❌')) fail('folded prompt must name the exact [qa] [report] ❌ marker')
    if (!p.includes('a listed branch has no open PR: move it to Blocked')) fail('folded prompt lost the no-PR tainted Blocked path')
    // Fails closed: non-zero check, fetch failure, sandbox marker, or a check that cannot complete.
    for (const w of ['exits non-zero', 'sandbox-10', 'cannot be completed']) if (!p.includes(w)) fail(`folded taint check lost "${w}"`)
    // The branch lookup and the single untainted rule (W40): no PR branch is not the same as no branch.
    for (const w of ["ls-remote --heads origin 'issue-10' 'issue-10-*'", 'It is untainted only when no branch is listed or every listed branch prints clean, and the marker is absent']) if (!p.includes(w)) fail(`folded taint check lost "${w}"`)
    // One shared preamble, one worktree, one cleanup, one report block.
    const once = (needle) => { const n = p.split(needle).length - 1; if (n !== 1) fail(`folded prompt has ${n}x "${needle}", wanted 1`) }
    once('First operation, before worktree creation'); once('Create ONE worktree'); once('Clean up the worktree once'); once('Report ONE result')
    if (p.includes('Report your exit via structured output')) fail('folded prompt still carries a per-lane exit block')
  }

  // On: a feature card and a qa card are not folded.
  same('on/feature', lanes((await run({ ...base, cards: feature, foldBuildQa: true })).calls), ['build', 'qa', 'review'])
  same('on/qa', lanes((await run({ ...base, cards: qa, foldBuildQa: true })).calls), ['qa', 'review'])

  // On: a sensitive bug card needs a separate read-only tester, so it is never folded.
  for (const l of ['money', 'payments', 'billing', 'pricing', 'auth', 'security', 'schema', 'destructive']) {
    const sensitive = [{ number: 15, status: 'Ready', title: 's', labels: ['bug', l] }]
    same(`on/bug+${l}`, lanes((await run({ ...base, cards: sensitive, foldBuildQa: true })).calls), ['build', 'qa', 'review'])
  }

  // The folded prompt defers taint to run.md; the QA worktree is not checked out twice.
  {
    const { calls } = await run({ ...base, cards: bug, foldBuildQa: true })
    const p = calls.find((x) => x.label.startsWith('build+qa')).prompt
    if (!p.includes('check taint (run.md → Tainted cards), fail closed')) fail('folded prompt must run the run.md taint check, fail closed')
    if (!p.includes('except its separate -qa worktree checkout')) fail('folded prompt must skip the QA lifecycle -qa worktree checkout')
  }

  // A qa label wins over bug, with or without an explicit lane.
  const bugqa = [{ number: 13, status: 'Ready', title: 'bq', labels: ['bug', 'qa'] }]
  same('on/bug+qa', lanes((await run({ ...base, cards: bugqa, foldBuildQa: true })).calls), ['qa', 'review'])
  const bugqaBuild = [{ number: 14, status: 'Ready', title: 'bq', labels: ['bug', 'qa'], lane: 'build' }]
  same('on/bug+qa/lane=build', lanes((await run({ ...base, cards: bugqaBuild, foldBuildQa: true })).calls), ['build', 'qa', 'review'])

  // advanced but not in Review: the chain stops, no review agent.
  {
    const { calls } = await run({ ...base, cards: bug, foldBuildQa: true }, { status: 'advanced', column: 'QA', detail: 'stuck in QA' })
    same('on/advanced-but-QA', lanes(calls), ['build+qa'])
  }

  // On: a bounced (tainted) or failed combined lane never reaches Review.
  for (const r of [
    { status: 'bounced', column: 'Ready', detail: 'tainted, rerun unfolded: x' },
    { status: 'failed', column: 'QA', detail: 'boom' },
    { status: 'blocked', column: 'Blocked', detail: 'stuck' },
  ]) {
    const { calls, out } = await run({ ...base, cards: bug, foldBuildQa: true }, r)
    same(`on/${r.status}`, lanes(calls), ['build+qa'])
    if (out.cards[0].finalStatus !== r.status) fail(`${r.status}: finalStatus ${out.cards[0].finalStatus}`)
  }

  // Fold-off prompts are byte-identical to 79b4142 except qa (W42 marker sentence) and build/qa (#67 exit checkpoint, default on); those hashes are re-taken, review is unchanged.
  {
    const cards = [
      { number: 7, status: 'Ready', title: 't', labels: ['bug'] }, { number: 8, status: 'QA', title: 'q', labels: ['feature'] },
      { number: 9, status: 'Ready', title: 'x', labels: ['qa'], lane: 'qa' }, { number: 10, status: 'Review', title: 'r' },
    ]
    const { calls } = await run({ ...base, cards })
    const got = calls.filter((c) => /^(build|qa|review):/.test(c.label))
      .map((c) => c.label + ' ' + crypto.createHash('sha256').update(c.prompt).digest('hex').slice(0, 16)).sort().join('\n')
    const want = ['build:#7 f68bbc596e3aed5d', 'qa:#7 7a5c1d9308961376', 'qa:#8 70b0696812d9a3ed', 'qa:#9 a77cd0555dbf001c',
      'review:#10 036abfa7ef22d597', 'review:#7 b8c69955147cd387', 'review:#8 5495193019fc2620', 'review:#9 4dcaa9c561f3b932'].join('\n')
    if (got !== want) fail(`switch-off prompts changed:\n${got}`)
  }

  console.log('OK: fold off equals today; fold on is bug-only, taint-first, and gates Review')
})().catch((e) => fail(e && e.stack || String(e)))
JS

# run.md carries the same rule the folded prompt points to.
RUN=../skills/super-board/references/run.md
grep -qF "ls-remote --heads origin 'issue-<N>' 'issue-<N>-*'" "$RUN" || { echo "FAIL: run.md Tainted cards lost the ls-remote branch lookup"; exit 1; }
grep -qF 'A card is untainted only when the lookup exits 0' "$RUN" || { echo "FAIL: run.md Tainted cards lost the single untainted rule"; exit 1; }
