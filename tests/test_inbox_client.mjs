import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = readFileSync('static/dashboard.html', 'utf8');

// The notifications drawer already owns `inbox` state and `refreshInbox()`, so the
// Telegram inbox lives under `mailbox`. The tab value stays 'inbox'.
test('the inbox is reachable from the sidebar and renders its own panel', () => {
  assert.match(html, /\{ tab: 'inbox', label: 'Inbox', icon: 'inbox', badge: 'inbox' \}/);
  assert.match(html, /v-show="tab === 'inbox'"/);
  assert.match(html, /inbox: \['Inbox', /);
  assert.match(html, /features: \{[^}]*inbox_enabled/);
  assert.match(html, /this\.features\.inbox_enabled = !!data\.inbox_enabled/);
  assert.match(html, /saveFeature\('inbox_enabled'/);
});

test('the icon registry carries an inbox glyph instead of falling back', () => {
  const match = /const APP_ICONS = (\{[\s\S]*?\n        \})/.exec(html);
  assert.ok(match, 'the icon registry must be readable');
  const icons = vm.runInNewContext(`(${match[1]})`);
  assert.ok(icons.inbox, 'APP_ICONS.inbox must be registered');
  assert.match(icons.inbox, /^<path/);
});

test('leaving the tab closes the inbox stream', () => {
  // The stream only opens once the config has answered whether the switch is on: starting
  // it beside the load left the live inbox permanently closed on a cold entry.
  assert.match(html, /if \(tab === 'inbox'\) \{\n\s+\/\/ The stream needs to know[\s\S]*?\n\s+this\.loadMailbox\(\)\.then\(\(\) => \{ if \(this\.tab === 'inbox'\) this\.startMailboxStream\(\) \}\)\n\s+\}/);
  assert.ok(!html.includes("this.loadMailbox(); this.startMailboxStream()"),
    'the stream must not start before the config is known');
  assert.match(html, /else this\.stopMailboxStream\(\)/);
  assert.match(html, /if \(this\.tab === 'inbox'\) this\.loadMailbox\(\)/);
});

test('a reply says only what the operator typed is sent', () => {
  assert.match(html, /Only a reply you type here is sent/);
});

test('the chats wall is a bounded list, not an endless scroll', () => {
  assert.match(html, /<bounded-list :items="mailbox\.chats" :limit="12">/);
  assert.match(html, /#default="\{ items: shownChats \}"/);
  assert.ok(!/max-height:520px;overflow:auto/.test(html), 'the scroll box must be gone');
});

function slice(start, end) {
  const from = html.indexOf(start);
  assert.ok(from >= 0, `missing ${start}`);
  const to = html.indexOf(end, from);
  assert.ok(to > from, `missing ${end} after ${start}`);
  return html.slice(from, to);
}

const mailboxMethods = slice('async loadMailbox() {', '// ---- Feature toggles ----');

const INBOX_METHODS = ['loadMailbox', 'setMailboxAccount', 'refreshMailbox', 'openMailboxChat',
  'markMailboxRead', 'sendMailboxReply', 'clearMailboxWall', 'applyMailboxEvent',
  'startMailboxStream', 'stopMailboxStream'];

test('every inbox method the panel calls exists', () => {
  for (const name of INBOX_METHODS) {
    assert.match(mailboxMethods, new RegExp(`${name}\\(`), `the panel must define ${name}`);
  }
});

test('the inbox methods reach only inbox endpoints', () => {
  const endpoints = [...mailboxMethods.matchAll(/_api\.(?:get|post|put|delete)\(\s*[`'"]([^`'"]+)/g)].map((m) => m[1]);
  assert.ok(endpoints.length >= 6, `expected the inbox routes to be called, saw ${endpoints.length}`);
  for (const url of endpoints) assert.match(url, /^\/api\/inbox\//, `the inbox must not call ${url}`);
  for (const forbidden of ['/api/bulk', '/api/schedules', '/api/auto-join', '/api/adder', '/api/scraper', '/api/auto-reply', '/api/notifications']) {
    assert.ok(!mailboxMethods.includes(forbidden), `the inbox must not call ${forbidden}`);
  }
});

test('the live stream is read with a bearer header, not EventSource', () => {
  // EventSource cannot carry the Authorization header, so the panel uses fetch + a reader.
  assert.ok(!mailboxMethods.includes('new EventSource'), 'EventSource cannot authenticate');
  assert.match(mailboxMethods, /new URL\('\/api\/inbox\/stream', _apiOrigin\)/);
  assert.match(mailboxMethods, /Authorization: 'Bearer ' \+ this\.session\.access_token/);
  assert.match(mailboxMethods, /response\.body\.getReader\(\)/);
  assert.match(mailboxMethods, /split\('\\n\\n'\)/);
});

function freshState() {
  return {
    config: { success: true, enabled: true, accounts: [], limit: 8, state: {}, watched: [], chats: 0,
      subscribers: 0, skipped_unknown: 0, labels: { lost: 'The connection stopped.' }, buckets: ['personal', 'group', 'channel', 'bot'] },
    chats: [], bucket: '', account: '', query: '', ref: '',
    messages: [], reply: '', loading: false, busy: '', error: '',
    media: {}, mediaError: {},
    live: false, streamError: '',
  };
}

const REFUSALS = {
  '/api/inbox/config': 'Inbox is switched off. Turn it on under Integrations > Platform switches.',
  '/api/inbox/refresh': 'Could not reach Telegram.',
  '/messages': 'That person asked not to be messaged. The reply is blocked.',
};

function harness({ responses = {}, fail = [] } = {}) {
  const calls = [];
  const toasts = [];
  const requests = [];
  const settled = (url, body, fallback) => {
    if (fail.some((needle) => url.includes(needle))) {
      const error = new Error('refused');
      error.response = { data: { detail: REFUSALS[fail.find((needle) => url.includes(needle))] } };
      throw error;
    }
    calls.push(url);
    requests.push({ url, body });
    return { data: responses[url] ?? fallback };
  };
  const defaults = {
    '/api/inbox/config': freshState().config,
    '/api/inbox/chats': { success: true, chats: [] },
  };
  const context = {
    _api: {
      get: async (url, config) => {
        if (config?.params) requests.push({ url, params: config.params });
        if (url.endsWith('/messages')) return settled(url, null, { success: true, messages: [], state: 'live' });
        return settled(url, null, defaults[url] ?? { success: true });
      },
      post: async (url, body) => {
        if (url.endsWith('/messages')) return settled(url, body, { success: true, message: { id: 9, text: 'typed', time: 1.5, outgoing: true, sender: '' } });
        if (url.endsWith('/refresh')) return settled(url, body, { success: true, chats: 4, skipped_unknown: 1 });
        return settled(url, body, { success: true });
      },
      put: async (url, body) => settled(url, body, { success: true, enabled: true, accounts: ['ui-one'], limit: 8, state: { 'ui-one': 'live' }, watched: [], chats: 0, subscribers: 0, skipped_unknown: 0, labels: {}, buckets: [] }),
      delete: async (url) => settled(url, null, { success: true }),
    },
    _apiOrigin: 'http://localhost',
    _mailboxStream: null,
    // Module-scope constant: the extracted methods reference it but do not define it.
    MAX_MAILBOX_MEDIA: Number(/const MAX_MAILBOX_MEDIA = (\d+)/.exec(html)?.[1]),
    console,
    JSON,
    Date,
    Math,
    AbortController,
    setTimeout: (fn) => fn,
    clearTimeout: () => {},
    TextDecoder: class { decode() { return ''; } },
    fetch: async () => { throw new Error('no network in the guard test'); },
  };
  const state = { mailbox: freshState(), session: { access_token: 'tok' }, showToast: (message, error) => toasts.push({ message, error }) };
  const methods = vm.runInNewContext(`({${mailboxMethods}})`, context);
  for (const [name, fn] of Object.entries(methods)) state[name] = fn.bind(state);
  return { state, calls, toasts, requests };
}

// Objects built inside the vm belong to another realm, so compare their JSON, not their identity.
const plain = (value) => JSON.parse(JSON.stringify(value ?? null));

test('loadMailbox reads the config and then the filtered wall', async () => {
  const { state, calls, requests } = harness({ responses: { '/api/inbox/chats': { success: true, chats: [{ ref: 'r1', name: 'Alice', bucket: 'personal', unread: 2 }] } } });
  state.mailbox.bucket = 'personal';
  state.mailbox.account = 'ui-one';
  await state.loadMailbox();
  assert.deepEqual(calls, ['/api/inbox/config', '/api/inbox/chats']);
  assert.deepEqual(plain(requests.find((r) => r.url === '/api/inbox/chats').params), { bucket: 'personal', account: 'ui-one', q: '' });
  assert.equal(state.mailbox.chats.length, 1);
  assert.equal(state.mailbox.error, '');
  assert.equal(state.mailbox.loading, false);
});

test('a refused inbox shows the server sentence instead of an empty wall', async () => {
  const { state } = harness({ fail: ['/api/inbox/config'] });
  await state.loadMailbox();
  assert.match(state.mailbox.error, /Inbox is switched off/);
  assert.equal(state.mailbox.loading, false);
});

test('opting an account in sends the flag and reloads the config it returns', async () => {
  const { state, calls, requests, toasts } = harness({
    responses: { '/api/inbox/config': { ...freshState().config, accounts: ['ui-one'], state: { 'ui-one': 'live' } } },
  });
  await state.setMailboxAccount('ui-one', true);
  assert.ok(calls.includes('/api/inbox/accounts/ui-one'));
  assert.deepEqual(plain(requests.find((r) => r.url === '/api/inbox/accounts/ui-one').body), { enabled: true });
  assert.deepEqual(plain(state.mailbox.config.accounts), ['ui-one']);
  assert.equal(state.mailbox.live, false, 'opting in does not open a second stream');
  assert.equal(toasts.length, 1);
  assert.equal(state.mailbox.busy, '');
});

test('an account id with a slash cannot escape its path segment', async () => {
  const { state, calls } = harness();
  await state.setMailboxAccount('a/b', true);
  assert.ok(calls.includes('/api/inbox/accounts/a%2Fb'), calls.join(' '));
});

test('refresh tells the operator how many chats were left out', async () => {
  const { state, calls, toasts } = harness();
  await state.refreshMailbox();
  assert.deepEqual(plain(calls), ['/api/inbox/refresh', '/api/inbox/config', '/api/inbox/chats']);
  assert.match(toasts[0].message, /4 chats loaded\. 1 left out as unclassifiable\./);
  assert.equal(state.mailbox.error, '');
  assert.equal(state.mailbox.busy, '');
});

test('opening a chat reads oldest first and clears the unread count', async () => {
  const { state, calls } = harness({
    responses: {
      '/api/inbox/chats/r1/messages': { success: true, state: 'live', messages: [{ id: 3, text: 'new', time: 3, outgoing: false, sender: '' }, { id: 1, text: 'old', time: 1, outgoing: false, sender: '' }] },
    },
  });
  state.mailbox.chats = [{ ref: 'r1', name: 'Alice', unread: 3 }];
  await state.openMailboxChat(state.mailbox.chats[0]);
  assert.equal(state.mailbox.ref, 'r1');
  assert.deepEqual(plain(state.mailbox.messages.map((m) => m.text)), ['old', 'new']);
  assert.equal(state.mailbox.chats[0].unread, 0);
  assert.ok(calls.includes('/api/inbox/chats/r1/read'), 'opening a chat marks it read');
});

test('the reply box sends only trimmed text and appends what went out', async () => {
  const { state, calls, requests } = harness();
  state.mailbox.ref = 'r1';
  state.mailbox.messages = [];
  await state.sendMailboxReply();
  assert.equal(calls.length, 0, 'an empty reply must not reach the server');
  state.mailbox.reply = '  Hi there  ';
  await state.sendMailboxReply();
  assert.deepEqual(plain(calls), ['/api/inbox/chats/r1/messages']);
  assert.deepEqual(plain(requests[requests.length - 1].body), { text: 'Hi there' });
  assert.equal(state.mailbox.messages[0].outgoing, true);
  assert.equal(state.mailbox.reply, '');
});

test('a refused send keeps the text on screen for the operator', async () => {
  const { state, calls } = harness({ fail: ['/messages'] });
  state.mailbox.ref = 'r1';
  state.mailbox.reply = 'hello';
  await state.sendMailboxReply();
  assert.match(state.mailbox.error, /asked not to be messaged/);
  assert.equal(state.mailbox.reply, 'hello', 'a refused reply keeps its text');
  assert.equal(calls.length, 0);
});

test('clearing the wall empties the screen and leaves the opt-ins alone', async () => {
  const { state, calls } = harness();
  state.mailbox.chats = [{ ref: 'r1' }];
  state.mailbox.ref = 'r1';
  state.mailbox.messages = [{ id: 1 }];
  await state.clearMailboxWall();
  assert.deepEqual(plain(calls), ['/api/inbox/cache']);
  assert.equal(state.mailbox.chats.length, 0);
  assert.equal(state.mailbox.ref, '');
  assert.equal(state.mailbox.messages.length, 0);
});

test('a stream frame updates the wall without a round trip', () => {
  const { state } = harness();
  state.mailbox.chats = [{ ref: 'r1', name: 'Alice', unread: 0, preview: 'old' }];
  state.mailbox.ref = 'r2';
  state.applyMailboxEvent({ type: 'ping' });
  assert.equal(state.mailbox.chats.length, 1);

  state.applyMailboxEvent({ type: 'chat', chat: { ref: 'r2', name: 'Bob', bucket: 'group', unread: 1, preview: 'hey', time: 5, account_id: 'ui-one' } });
  assert.equal(state.mailbox.chats[0].ref, 'r2', 'a new chat leads the wall');

  state.applyMailboxEvent({ type: 'chat', chat: { ref: 'r1', name: 'Alice', bucket: 'personal', unread: 4, preview: 'newest', time: 9, account_id: 'ui-one' } });
  assert.equal(state.mailbox.chats.find((c) => c.ref === 'r1').preview, 'newest');
  assert.equal(state.mailbox.chats.find((c) => c.ref === 'r1').unread, 4);

  state.applyMailboxEvent({ type: 'message', ref: 'r2', message: { id: 7, text: 'live', time: 6, outgoing: false, sender: '' } });
  assert.equal(state.mailbox.messages.length, 1);

  state.applyMailboxEvent({ type: 'message', ref: 'r1', message: { id: 8, text: 'other chat', time: 6, outgoing: false, sender: '' } });
  assert.equal(state.mailbox.messages.length, 1, 'a thread not on screen is not appended');

  state.applyMailboxEvent({ type: 'read', ref: 'r1', unread: 0 });
  assert.equal(state.mailbox.chats.find((c) => c.ref === 'r1').unread, 0);

  state.applyMailboxEvent({ type: 'state', state: 'slowed' });
  assert.match(state.mailbox.streamError, /fell behind/);
  assert.equal(state.mailbox.live, false, 'a stalled tab stops its own stream');
});

test('the ready frame replaces the config the panel started with', () => {
  const { state } = harness();
  state.applyMailboxEvent({ type: 'ready', state: { enabled: true, accounts: ['ui-one'], labels: {}, buckets: [], skipped_unknown: 2, limit: 8, state: {}, watched: [], chats: 1, subscribers: 1, success: true } });
  assert.deepEqual(state.mailbox.config.accounts, ['ui-one']);
  assert.equal(state.mailbox.config.skipped_unknown, 2);
});
