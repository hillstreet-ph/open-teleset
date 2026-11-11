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
const targetMethods = methodSource('parseScrapeTargets(raw) {', 'async runChannelScraper() {');
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
    scraper: { accountId: 'acct_1', target: '@channel', bulkTargets: '', limit: 200, filterBots: true, activityFilter: 'all', loading: false, results: [], summary: '' },
    scraperTargets: ['@channel'],
    channelScraper: {
      keywords: 'casino', country: '', language: '', proxyMode: 'none', proxyUrls: '',
      groupType: 'all', limit: 100, loading: false, results: [], summary: '', partial: false,
      diagnostics: null,
    },
    showToast: (message, error) => toasts.push({ message, error }),
  };
  const methods = vm.runInNewContext(`({${source}})`, context);
  for (const [name, fn] of Object.entries(methods)) state[name] = fn.bind(state);
  return { state, calls, toasts, alerts };
}

test('a hidden member list reports recent posters instead of claiming a full roster', async () => {
  const notice = 'Telegram hides the full member list from non-admins for this target, so this lists the people who posted recently instead. Telegram reports 1,500 members.';
  const { state } = harness(scraperMethods, async () => ({
    data: {
      members: [{ id: 11, username: 'ana' }], total: 1, scanned: 1, excluded: 0,
      source: 'recent_posters', limited: true, member_count: 1500, notice,
    },
  }));
  await state.runScraper();
  assert.equal(state.scraper.results.length, 1);
  assert.equal(state.scraper.source, 'recent_posters');
  assert.equal(state.scraper.notice, notice);
  assert.match(state.scraper.summary, /recent posters/i);
  assert.match(html, /scraper\.notice/);
  assert.match(html, /Recent posters/);
});

test('workflow results label recent posters as partial, not members', () => {
  const methods = vm.runInNewContext(`({${methodSource('stepSummary(step) {', 'async loadWorkflows() {')}})`, {});
  const partial = methods.stepSummary({ status: 'succeeded', detail: {
    scraped: 2, source: 'recent_posters', limited: true,
  } });
  assert.match(partial, /2 recent posters/);
  assert.match(partial, /partial/i);
  assert.doesNotMatch(partial, /members read/);
  assert.equal(methods.stepSummary({ status: 'succeeded', detail: { scraped: 2 } }), '2 members read');
});

test('channel search sends the language hint and the proxy choice', async () => {
  const { state, calls } = harness(channelMethods, async () => ({ data: { results: [], total: 0 } }));
  state.channelScraper.country = 'Philippines';
  state.channelScraper.language = 'tagalog';
  state.channelScraper.proxyMode = 'custom';
  state.channelScraper.proxyUrls = '  http://user:pass@a.example:8080 \n\n https://b.example:8443 ';
  await state.runChannelScraper();
  assert.equal(calls[0].body.language, 'tagalog');
  assert.equal(calls[0].body.proxy_mode, 'custom');
  assert.deepEqual([...calls[0].body.proxy_urls], [
    'http://user:pass@a.example:8080', 'https://b.example:8443',
  ]);
});

test('custom proxy mode without URLs stops before the request', async () => {
  const { state, calls, toasts } = harness(channelMethods, async () => ({ data: { results: [], total: 0 } }));
  state.channelScraper.proxyMode = 'custom';
  state.channelScraper.proxyUrls = '   ';
  await state.runChannelScraper();
  assert.equal(calls.length, 0);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0].message, /proxy URL/i);
  assert.equal(state.channelScraper.loading, false);
});

test('non-custom modes send an empty proxy list', async () => {
  const { state, calls } = harness(channelMethods, async () => ({ data: { results: [], total: 0 } }));
  state.channelScraper.proxyMode = 'pool';
  state.channelScraper.proxyUrls = 'http://ignored.example:8080';
  await state.runChannelScraper();
  assert.equal(calls[0].body.proxy_mode, 'pool');
  assert.deepEqual([...calls[0].body.proxy_urls], []);
});

test('zero channel results use the server reason instead of a bare zero', async () => {
  const { state } = harness(channelMethods, async () => ({
    data: {
      results: [], total: 0,
      diagnostics: { mode: 'public', reason: 'TGStat did not answer any keyword. Check TGSTAT_TOKEN and try again.' },
    },
  }));
  await state.runChannelScraper();
  assert.match(state.channelScraper.summary, /Found 0 public channels\/groups/);
  assert.match(state.channelScraper.summary, /TGSTAT_TOKEN/);
  assert.equal(state.channelScraper.diagnostics.mode, 'public');
});

test('bulk target parsing accepts spreadsheets, comments and de-duplicates usernames', () => {
  const methods = vm.runInNewContext(`({${targetMethods}})`, {});
  const parsed = methods.parseScrapeTargets.call({}, [
    'https://t.me/AlphaChannel',
    '@alphaCHANNEL   # duplicate link',
    'secondgroup, -1001234567890; thirdgroup',
    '"https://t.me/+InviteHash_123"',
    'https://t.me/+invitehash_123',
    '-1001234567890',
    '',
  ].join('\n'));
  assert.deepEqual([...parsed.unique], [
    'https://t.me/AlphaChannel', 'secondgroup', '-1001234567890', 'thirdgroup',
    'https://t.me/+InviteHash_123',
  ]);
  assert.equal(parsed.duplicates, 3);
});

test('finder results merge into bulk targets without duplicates', () => {
  const methods = vm.runInNewContext(`({${targetMethods}})`, {});
  const state = {
    scraper: { bulkTargets: '@alpha\n@beta' },
    channelScraper: { results: [
      { username: 'alpha' }, { username: 'gamma' }, { username: '' , url: 'https://t.me/+joinhash1' },
    ] },
    showToast: () => {},
  };
  Object.assign(state, {
    parseScrapeTargets: methods.parseScrapeTargets.bind(state),
    useChannelResultsAsTargets: methods.useChannelResultsAsTargets.bind(state),
  });
  state.useChannelResultsAsTargets();
  assert.equal(state.scraper.bulkTargets, '@alpha\n@beta\n@gamma\nhttps://t.me/+joinhash1');
});

test('a normal roster clears a previous degraded notice', async () => {
  const { state } = harness(scraperMethods, async () => ({
    data: { members: [{ id: 11, username: 'ana' }], total: 1, scanned: 1, excluded: 0, source: 'members' },
  }));
  state.scraper.notice = 'stale notice';
  state.scraper.source = 'recent_posters';
  await state.runScraper();
  assert.equal(state.scraper.notice, '');
  assert.equal(state.scraper.source, 'members');
});

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

test('channel search is account-free and never sends a Telegram account', async () => {
  const { state, calls } = harness(channelMethods, async () => ({ data: { results: [], total: 0 } }));
  state.channelScraper.keywords = 'alpha\nbravo\nalpha';
  await state.runChannelScraper();
  assert.equal(calls[0].body.account_id, '');
  assert.deepEqual([...calls[0].body.keywords], ['alpha', 'bravo', 'alpha']);
  assert.match(state.channelScraper.summary, /from 3 keywords/);
  assert.equal(state.channelScraper.partial, false);
});

test('a partial channel search says keywords were skipped', async () => {
  const { state } = harness(channelMethods, async () => ({
    data: { results: [{ id: 'casino', username: 'casino' }], total: 1, partial: true },
  }));
  await state.runChannelScraper();
  assert.equal(state.channelScraper.partial, true);
  assert.equal(state.channelScraper.results.length, 1);
  assert.match(html, /channelScraper\.partial/);
  assert.match(html, /results are partial/i);
});

test('the channel finder no longer offers a Telegram account selector', () => {
  const start = html.indexOf('Channel and group finder');
  const end = html.indexOf('<!-- ============ Adder Tab', start);
  const finder = html.slice(start, end);
  assert.doesNotMatch(finder, /Account \(optional\)/);
  assert.doesNotMatch(finder, /channelScraper\.accountId/);
  assert.match(finder, /No Telegram account is used/);
  assert.match(finder, /max="10000"/);
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
