#!/usr/bin/env node
// Project-owned Playwright and scenario; no dependencies installed by Super Board.
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {writeFile, mkdir} from 'node:fs/promises';
import {performance} from 'node:perf_hooks';

let browser;
try {
  const project = process.env.SB_RUNTIME_PROJECT;
  if (!project) throw new Error('SB_RUNTIME_PROJECT must point to the owning app with Playwright installed');
  const require = createRequire(resolve(project, 'package.json'));
  const {chromium} = require('playwright');
  const scenarioPath = resolve(process.argv[2]);
  const {run} = await import(pathToFileURL(scenarioPath).href);
  if (typeof run !== 'function') throw new Error('scenario module must export run(page)');
  browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1280, height: 720}, locale: 'en-US', timezoneId: 'UTC'});
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('console', msg => {if (msg.type() === 'error') errors.push(msg.text());});
  const cdp = await context.newCDPSession(page);
  await cdp.send('Performance.enable');
  await page.goto(process.env.SB_RUNTIME_URL, {waitUntil: 'networkidle'});
  await cdp.send('HeapProfiler.collectGarbage');
  const before = await cdp.send('Performance.getMetrics');
  // Trace/screenshot/CDP sampling never run inside the timed action.
  const started = performance.now();
  const result = await run(page);
  const duration = performance.now() - started;
  await cdp.send('HeapProfiler.collectGarbage');
  const after = await cdp.send('Performance.getMetrics');
  const memory = after.metrics.find(row => row.name === 'JSHeapUsedSize')?.value;
  const beforeMemory = before.metrics.find(row => row.name === 'JSHeapUsedSize')?.value;
  const dir = process.env.SB_RUNTIME_ARTIFACT_DIR;
  await mkdir(dir, {recursive: true});
  await writeFile(resolve(dir, 'cdp-metrics.json'), JSON.stringify({before, after, errors}, null, 2));
  await page.screenshot({path: resolve(dir, 'result.png')});
  console.log(JSON.stringify({correct: result?.correct === true && errors.length === 0,
    duration_ms: duration, memory_bytes: memory, memory_before_bytes: beforeMemory,
    environment: {browser: 'chromium', version: browser.version(), headless: true,
      viewport: '1280x720', locale: 'en-US', timezone: 'UTC', timing_scope: 'automation action, excludes launch/navigation/tracing'},
    detail: result, page_errors: errors.length}));
} catch (error) {
  console.log(JSON.stringify({correct: false, error: String(error)}));
  process.exitCode = 2;
} finally {
  await browser?.close();
}
