import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const method = html.slice(html.indexOf('async signInWithOAuth(provider) {'), html.indexOf('async sendPasswordReset() {'));

function harness(external = {}, fetchError = null, sdkError = null) {
  const calls = [];
  const context = {
    window: { location: { origin: 'https://open-teleset.site' }, OPEN_TELESET_CONFIG: {
      supabaseUrl: 'https://project.supabase.co/', supabaseAnonKey: 'public-key'
    } },
    AbortSignal,
    fetch: async (url, init) => {
      calls.push({ url, init });
      if (fetchError) throw fetchError;
      return { ok: true, json: async () => ({ external }) };
    },
    _supabase: { auth: { signInWithOAuth: async args => { calls.push(args); return { error: sdkError }; } } }
  };
  const state = { authSubmitting: false, authError: 'old error', authSuccess: 'old success', authEmail: '' };
  state.signInWithOAuth = vm.runInNewContext(`({${method}}).signInWithOAuth`, context);
  return { state, calls };
}

test('disabled provider stays on dashboard with actionable error', async () => {
  const { state, calls } = harness({ google: false });
  await state.signInWithOAuth('google');
  assert.equal(calls.length, 1);
  assert.match(state.authError, /Google sign-in is not configured/);
  assert.equal(state.authSubmitting, false);
  assert.equal(state.authSuccess, '');
});

test('Google uses current dashboard origin and account chooser for either email', async () => {
  for (const email of ['tanauancharles1@gmail.com', 'kairocasino8@gmail.com']) {
    const { state, calls } = harness({ google: true });
    state.authEmail = email;
    await state.signInWithOAuth('google');
    assert.equal(calls[0].url, 'https://project.supabase.co/auth/v1/settings');
    assert.equal(calls[0].init.headers.apikey, 'public-key');
    assert.equal(calls[1].options.redirectTo, 'https://open-teleset.site/dashboard');
    assert.equal(calls[1].options.queryParams.prompt, 'select_account');
    assert.equal(calls[1].options.queryParams.login_hint, email);
    assert.equal(state.authError, '');
  }
});

test('GitHub requests email identity without repository access', async () => {
  const { state, calls } = harness({ github: true });
  await state.signInWithOAuth('github');
  assert.equal(calls[1].provider, 'github');
  assert.equal(calls[1].options.scopes, 'user:email');
  assert.equal(calls[1].options.queryParams, undefined);
});

test('network and SDK failures release the button for retry', async () => {
  for (const [network, sdk] of [[new Error('Network failed'), null], [null, new Error('OAuth failed')]]) {
    const { state } = harness({ google: true }, network, sdk);
    await state.signInWithOAuth('google');
    assert.match(state.authError, /failed/);
    assert.equal(state.authSubmitting, false);
  }
});

test('duplicate submission and unknown providers never start OAuth', async () => {
  const { state, calls } = harness({ google: true });
  state.authSubmitting = true;
  await state.signInWithOAuth('google');
  assert.equal(calls.length, 0);
  state.authSubmitting = false;
  await state.signInWithOAuth('unknown');
  assert.equal(calls.length, 0);
  assert.match(state.authError, /Unsupported/);
});
