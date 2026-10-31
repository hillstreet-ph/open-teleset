import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

function methodSource(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = html.indexOf(endMarker, start);
  assert.ok(end > start, `missing end marker after ${startMarker}`);
  return html.slice(start, end);
}

const scraperMethods = methodSource('async runScraper() {', 'copyScrapedUsernames() {');
const channelMethods = methodSource('async runChannelScraper() {', 'exportChannelCSV() {');
const rowCaps = methodSource('scraperRows() {', 'autojoinLinks() {');

function harness(source, post) {
  const calls = [];
  const toasts = [];
  const alerts = [];
  const context = {
    _api: { post: async (url, body, config) => { calls.push({ url, body, config }); return post(); } },
    alert: message => alerts.push(message),
    console,
  };
  const state = {
    scraper: { accountId: 'acct_1', target: '@channel', limit: 200, filterBots: true, activityFilter: 'all', loading: false, results: [], summary: '' },
    channelScraper: { accountId: '', keywords: 'casino', country: '', groupType: 'all', limit: 100, loading: false, results: [], summary: '' },
    showToast: (message, error) => toasts.push({ message, error }),
  };
  const methods = vm.runInNewContext(`({${source}})`, context);
  for (const [name, fn] of Object.entries(methods)) state[name] = fn.bind(state);
  return { state, calls, toasts, alerts };
}

test('member scrape allows Telegram more than the default 15s request budget', async () => {
  const { state, calls } = harness(scraperMethods, async () => ({ data: { members: [], total: 0, scanned: 0, excluded: 0 } }));
  await state.runScraper();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/scraper/members');
  assert.ok((calls[0].config?.timeout || 0) >= 60000, `timeout was ${calls[0].config?.timeout}`);
});

test('channel search allows directory lookups more than the default 15s request budget', async () => {
  const { state, calls } = harness(channelMethods, async () => ({ data: { results: [], total: 0 } }));
  await state.runChannelScraper();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/scraper/channels');
  assert.ok((calls[0].config?.timeout || 0) >= 60000, `timeout was ${calls[0].config?.timeout}`);
});

test('member scrape shows the server reason in a toast instead of a blocking alert', async () => {
  const { state, alerts, toasts } = harness(scraperMethods, async () => {
    throw { response: { data: { detail: 'Could not find @ghost. Check the username or ID.' } } };
  });
  await state.runScraper();
  assert.equal(alerts.length, 0);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0].message, /could not find/i);
  assert.equal(toasts[0].error, true);
  assert.equal(state.scraper.loading, false);
});

test('request timeout becomes a clear message rather than axios internals', async () => {
  const { state, alerts, toasts } = harness(scraperMethods, async () => {
    const error = new Error('timeout of 90000ms exceeded');
    error.code = 'ECONNABORTED';
    throw error;
  });
  await state.runScraper();
  assert.equal(alerts.length, 0);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0].message, /timed out/i);
  assert.doesNotMatch(toasts[0].message, /90000|ECONNABORTED/);
});

test('scraper tables render at most 200 rows while keeping every result for export', () => {
  const methods = vm.runInNewContext(`({${rowCaps}})`, {});
  const state = {
    scraper: { results: Array.from({ length: 5000 }, (_, i) => ({ id: i })) },
    channelScraper: { results: Array.from({ length: 500 }, (_, i) => ({ id: i })) },
  };
  assert.equal(methods.scraperRows.call(state).length, 200);
  assert.equal(methods.channelRows.call(state).length, 200);
  assert.match(html, /v-for="\(m, idx\) in scraperRows"/);
  assert.match(html, /v-for="\(c, idx\) in channelRows"/);
  assert.match(html, /Export CSV for every row/);
});
