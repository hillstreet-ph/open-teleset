// The panel's half of the MCP OAuth sign-in: what it sends, what it keeps out of the
// address bar, and which state it shows. The methods run against a stubbed _api so a
// mistake in the flow fails here rather than in an operator's browser.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

// Objects built inside the vm sandbox have a different realm's prototype, which strict
// deep-equality rejects; compare their plain values instead.
const plain = (value) => JSON.parse(JSON.stringify(value));

function panel({ search = '', responses = {}, failures = {} } = {}) {
  const calls = []
  const navigations = []
  const addressBar = { search, pathname: '/dashboard' }
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (failures[url]) { const error = new Error('refused'); error.response = { data: { detail: failures[url] } }; return Promise.reject(error) }
    return Promise.resolve({ data: responses[url] ?? (method === 'GET' ? { integrations: [], suggested: [] } : { success: true }) })
  }
  const api = {
    get: (url) => respond('GET', url, undefined),
    put: (url, body) => respond('PUT', url, body),
    post: (url, body) => respond('POST', url, body),
    delete: (url) => respond('DELETE', url, undefined),
  }
  const sandbox = {
    _api: api,
    Date,
    Math,
    JSON,
    Number,
    URLSearchParams,
    window: {
      location: { ...addressBar, assign: (url) => navigations.push(url) },
      history: { replaceState: (_s, _t, url) => { addressBar.search = url.includes('?') ? url.slice(url.indexOf('?')) : '' } },
      confirm: () => true,
    },
  }
  const start = html.indexOf('async loadIntegrations() {')
  const end = html.indexOf('// ---- Feature toggles ----', start)
  assert.ok(start > 0 && end > start, 'integration methods not found')
  const methods = vm.runInNewContext(`({${html.slice(start, end).replace(/,\s*$/, '')}})`, sandbox)
  const toasts = []
  const state = {
    tab: 'overview',
    toasts,
    pendingOAuthReturn: null,
    integrations: {
      loading: false, list: [], suggested: [], error: '', notice: '', busy: '', editing: '', redirectUri: '',
      form: { name: '', url: '', transport: 'http', token: '', auth_mode: 'token', client_id: '', client_secret: '', scopes: '' },
    },
    showToast(message, isError) { toasts.push({ message, isError }) },
    go(tab) { this.tab = tab },
    ...methods,
  }
  return { state, calls, navigations, addressBar, sandbox }
}

const served = {
  '/api/integrations/mcp': {
    integrations: [{
      id: 'mcp-1', name: 'Example MCP', base_url: 'https://mcp.example.com/mcp', transport: 'http',
      has_token: false, created_at: '2026-11-01T00:00:00+00:00', auth_mode: 'oauth', authorized: false,
      expires_at: null, scopes: '', granted_scopes: '', has_client: false,
    }],
    suggested: [],
    redirect_uri: 'https://open-teleset.site/dashboard',
  },
}

test('a code in the address bar is taken out of it before anything else runs', () => {
  const { state, addressBar } = panel({ search: '?code=c-1&state=s-1&tab=extras' })
  state.captureOAuthReturn()

  assert.deepEqual(plain(state.pendingOAuthReturn), { code: 'c-1', state: 's-1' })
  assert.ok(!addressBar.search.includes('code='), 'the single-use code must leave the URL')
  assert.ok(!addressBar.search.includes('state='), 'the state must leave the URL')
  assert.ok(addressBar.search.includes('tab=extras'), 'unrelated parameters stay')
})

test('a return without a code is ignored rather than reported as a failure', () => {
  const { state } = panel({ search: '' })
  state.captureOAuthReturn()
  assert.equal(state.pendingOAuthReturn, null)
  assert.equal(state.toasts.length, 0)
})

test('the panel spends the code once and lands on the server list', async () => {
  const { state, calls } = panel({
    responses: { '/api/integrations/mcp': served['/api/integrations/mcp'], '/api/integrations/mcp/oauth/callback': { integration: { name: 'Example MCP' } } },
  })
  state.pendingOAuthReturn = { code: 'c-1', state: 's-1' }

  await state.spendOAuthReturn()

  const post = calls.find((call) => call.url === '/api/integrations/mcp/oauth/callback')
  assert.deepEqual(plain(post.body), { code: 'c-1', state: 's-1' })
  assert.equal(state.tab, 'integrations')
  assert.equal(state.integrations.notice, 'Signed in to Example MCP.')

  // A second call must not re-send a code the server has already spent.
  await state.spendOAuthReturn()
  assert.equal(calls.filter((call) => call.url === '/api/integrations/mcp/oauth/callback').length, 1)
})

test('a refused exchange tells the operator in the server list', async () => {
  const { state } = panel({ failures: { '/api/integrations/mcp/oauth/callback': 'That sign-in expired. Start again.' } })
  state.pendingOAuthReturn = { code: 'c-1', state: 's-1' }
  state.tab = 'integrations'

  await state.spendOAuthReturn()

  assert.equal(state.integrations.error, 'That sign-in expired. Start again.')
  assert.equal(state.toasts[0].isError, true)
  assert.equal(state.integrations.notice, '')
})

test('signing in hands the browser the address the server chose', async () => {
  const { state, navigations, calls } = panel({
    responses: { '/api/integrations/mcp/mcp-1/oauth/start': { authorization_url: 'https://auth.example.com/authorize?state=s-1' } },
  })

  await state.startIntegrationSignIn({ id: 'mcp-1' })

  assert.deepEqual(plain(calls[0]), { method: 'POST', url: '/api/integrations/mcp/mcp-1/oauth/start', body: {} })
  assert.deepEqual(navigations, ['https://auth.example.com/authorize?state=s-1'])
})

test('a server that fails to start a sign-in says so and stays usable', async () => {
  const { state, navigations } = panel({
    failures: { '/api/integrations/mcp/mcp-1/oauth/start': 'That server does not advertise an OAuth sign-in we can use.' },
  })

  await state.startIntegrationSignIn({ id: 'mcp-1' })

  assert.equal(state.integrations.error, 'That server does not advertise an OAuth sign-in we can use.')
  assert.equal(state.integrations.busy, '')
  assert.equal(navigations.length, 0)
})

test('an OAuth server is saved with its sign-in fields and no empty secret', () => {
  const { state, calls } = panel()
  state.integrations.form = {
    name: 'Example MCP', url: 'https://mcp.example.com/mcp', transport: 'sse', token: '  ',
    auth_mode: 'oauth', client_id: '  ', client_secret: '', scopes: ' mcp:tools ',
  }

  return state.saveIntegration().then(() => {
    const put = calls.find((call) => call.method === 'PUT')
    assert.deepEqual(plain(put.body), {
      name: 'Example MCP', base_url: 'https://mcp.example.com/mcp', transport: 'sse', auth_mode: 'oauth', scopes: 'mcp:tools',
    })
  })
})

test('editing keeps the server id and never re-sends a blank token', async () => {
  const { state, calls } = panel()
  state.integrations.list = served['/api/integrations/mcp'].integrations
  state.editIntegration(state.integrations.list[0])
  state.integrations.form.client_id = 'issued-client'

  await state.saveIntegration()

  const put = calls.find((call) => call.method === 'PUT')
  assert.equal(put.body.id, 'mcp-1')
  assert.equal(put.body.auth_mode, 'oauth')
  assert.equal(put.body.client_id, 'issued-client')
  assert.ok(!('api_key' in put.body), 'a blank token must not overwrite the stored one')
  assert.ok(!('client_secret' in put.body), 'a blank secret must not overwrite the stored one')
})

test('the badge says what the operator has left to do', () => {
  const { state } = panel()
  const base = { auth_mode: 'oauth', authorized: false, has_client: false, has_token: false, expires_at: null }

  assert.equal(state.integrationState(base).text, 'Needs sign-in')
  assert.equal(state.integrationState({ ...base, has_client: true }).text, 'Signed out')
  assert.equal(state.integrationState({ ...base, authorized: true, expires_at: new Date(Date.now() + 3600e3).toISOString() }).text, 'Signed in')
  assert.equal(state.integrationState({ ...base, authorized: true, expires_at: new Date(Date.now() + 60e3).toISOString() }).cls, 'badge-warning')
  assert.equal(state.integrationState({ ...base, auth_mode: 'token', has_token: true }).text, 'Token stored')
})

test('the server list carries the callback address the operator must whitelist', async () => {
  const { state, calls } = panel({ responses: { '/api/integrations/mcp': served['/api/integrations/mcp'] } })

  await state.loadIntegrations()

  assert.equal(state.integrations.redirectUri, 'https://open-teleset.site/dashboard')
  assert.equal(state.integrations.loading, false)
  assert.equal(calls[0].url, '/api/integrations/mcp')
})
