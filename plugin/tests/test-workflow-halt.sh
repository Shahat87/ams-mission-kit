#!/usr/bin/env bash
# Offline workflow boundary: a read halt ends queued lane chains and the wave.
set -euo pipefail
cd "$(dirname "$0")"
node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
async function run(cards, failAt, verdictFor = () => null) {
  const calls = []
  const agent = async (_, options) => {
    calls.push(options.label)
    if (options.label.startsWith('preflight:')) {
      return { verdicts: cards.filter(c => c.status === 'Ready').map(c => verdictFor(c) || ({ number: c.number,
        verdict: failAt === 'preflight' ? 'halted' : 'proceed', detail: 'required GitHub read failed', files: [] })) }
    }
    if (options.label.startsWith('classify:')) {
      const parked = verdictFor({ number: Number(options.label.split('#')[1]) })
      return {kind:'feature',complexity:'low',status: failAt === 'classify' || parked ? 'halted' : 'ok'}
    }
    return { status: options.label.startsWith(failAt + ':') ? 'halted' : 'advanced', column:'QA', detail:'result' }
  }
  const pipeline = async (items, ...stages) => {
    const results = []
    // Sequential scheduling makes queued work visible after a sibling trips the run.
    for (const item of items) {
      let result = await stages[0](item)
      for (const stage of stages.slice(1)) result = await stage(result, item)
      results.push(result)
    }
    return results
  }
  const output = await new AsyncFunction('args','agent','pipeline','log',source)(
    {configPath:'fixture.json',repo:'o/r',cards}, agent,pipeline,()=>{})
  return {calls, output}
}
const cards = [1,2].map(number => ({number,title:'Task',status:'Ready'}))
;(async () => {
  for (const failed of ['preflight','classify','build','qa']) {
    const {calls,output} = await run(cards,failed)
    if (!output.halted) throw Error(failed + ' must halt wave')
    if (calls.includes('review:#1') || calls.includes('build:#2')) throw Error('dispatched after halt: ' + calls)
    if (output.cards.some(c => c.finalStatus !== 'halted')) throw Error('halted cards must preserve stop status')
  }
  const {output} = await run(cards,'never')
  if (output.halted || output.cards.some(c => c.finalStatus !== 'advanced')) throw Error('healthy workflow changed')
  // A card pre-flight parked in Blocked is never classified, so its classify cannot halt the wave.
  const hold = c => c.number === 1 ? { number: 1, verdict: 'hold', column: 'Blocked', detail: 'unclear' } : null
  const parked = await run(cards, 'never', hold)
  if (parked.calls.includes('classify:#1')) throw Error('classified a parked card: ' + parked.calls)
  if (parked.output.halted) throw Error('a parked card halted the wave')
  const byNum = Object.fromEntries(parked.output.cards.map(c => [c.number, c]))
  if (byNum[1].finalStatus !== 'blocked' || byNum[1].column !== 'Blocked') throw Error('parked card must stay blocked: ' + JSON.stringify(byNum[1]))
  if (byNum[2].finalStatus !== 'advanced') throw Error('healthy card must still advance: ' + JSON.stringify(byNum[2]))
  console.log('PASS: workflow halt propagation (6 scenarios)')
})().catch(e => {console.error(e);process.exit(1)})
JS
