import { readFileSync } from 'node:fs';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

test('webhook create button stays enabled while idle', () => {
  // Vue renders disabled="" for an empty-string binding, so a busy id must be coerced.
  assert.match(html, /@click="createWebhook" :disabled="!!webhooks\.busy"/);
});

test('webhook panel surfaces one-time secrets, delivery health and all actions', () => {
  assert.match(html, /New signing secret — copy it now, it won't be shown again/);
  assert.match(html, /webhooks\.created = ''/);
  assert.match(html, /Last delivery \{\{ hookStamp\(hook\.last_attempt\) \}\}/);
  assert.match(html, /@click="testWebhook\(hook\)"/);
  assert.match(html, /@click="toggleWebhook\(hook\)"/);
  assert.match(html, /@click="rotateWebhook\(hook\)"/);
  assert.match(html, /@click="deleteWebhook\(hook\)"/);
});

test('webhook client methods use owner-scoped endpoints and refresh health', () => {
  for (const call of [
    "_api.get('/api/webhooks')",
    "_api.post('/api/webhooks', { name, url, events: this.webhooks.form.events })",
    "_api.patch('/api/webhooks/' + hook.id, patch)",
    "_api.post('/api/webhooks/' + hook.id + '/rotate')",
    "_api.post('/api/webhooks/' + hook.id + '/test', null, { timeout: 20000 })",
    "_api.delete('/api/webhooks/' + hook.id)",
  ]) {
    assert.ok(html.includes(call), `missing ${call}`);
  }
  assert.match(html, /if \(tab === 'integrations'\) this\.loadWebhooks\(\)/);
});
