import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const method = html.slice(html.indexOf('async signInWithOAuth(provider) {'), html.indexOf('async sendPasswordReset() {'));
const passwordMethod = html.slice(html.indexOf('async signIn() {'), html.indexOf('async signUp() {'));

function passwordHarness(aliases = {}, sdkError = null) {
  const calls = [];
  const context = {
    window: { OPEN_TELESET_CONFIG: { loginAliases: aliases } },
    _supabase: { auth: { signInWithPassword: async args => { calls.push(args); return { error: sdkError }; } } }
  };
  const state = { authSubmitting: false, authError: '', authSuccess: '', authEmail: '', authPassword: 'synthetic-password' };
  state.signIn = vm.runInNewContext(`({${passwordMethod}}).signIn`, context);
  return { state, calls };
}

test('email and configured username use the same Supabase password identity', async () => {
  for (const input of [' admin ', ' account@example.com ']) {
    const { state, calls } = passwordHarness({ admin: 'account@example.com' });
    state.authEmail = input;
    await state.signIn();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].email, 'account@example.com');
    assert.equal(calls[0].password, 'synthetic-password');
    assert.equal(state.authSubmitting, false);
    assert.equal(state.authError, '');
  }
});

test('unknown and inherited usernames cannot select an identity', async () => {
  for (const input of ['unknown', 'admin', 'constructor']) {
    const { state, calls } = passwordHarness(Object.create({ admin: 'account@example.com' }));
    state.authEmail = input;
    await state.signIn();
    assert.equal(calls.length, 0);
    assert.match(state.authError, /configured username/);
    assert.equal(state.authSubmitting, false);
  }
});

test('password SDK failure and duplicate submissions release or retain the guard', async () => {
  const { state, calls } = passwordHarness({}, new Error('Invalid credentials'));
  state.authEmail = 'account@example.com';
  state.authSubmitting = true;
  await state.signIn();
  assert.equal(calls.length, 0);
  state.authSubmitting = false;
  await state.signIn();
  assert.match(state.authError, /Invalid credentials/);
  assert.equal(state.authSubmitting, false);
});

function harness(external = {}, fetchError = null, sdkError = null) {
  const calls = [];
  const context = {
    window: { location: { origin: 'https://app.open-teleset.site' }, OPEN_TELESET_CONFIG: {
      supabaseUrl: 'https://project.supabase.co/', supabaseAnonKey: 'public-key', pagesOrigin: 'https://open-teleset.site'
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

test('Google returns to the canonical dashboard from the secondary host', async () => {
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

test('username does not become a Google email hint', async () => {
  const { state, calls } = harness({ google: true });
  state.authEmail = 'admin';
  await state.signInWithOAuth('google');
  assert.equal(calls[1].options.queryParams.login_hint, undefined);
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

const resetMethod = html.slice(html.indexOf('async sendPasswordReset() {'), html.indexOf('async updatePassword() {'));
test('password recovery returns to the canonical dashboard', async () => {
  const calls = [];
  const state = { authEmail: 'account@example.com', authError: '', authSuccess: '', authSubmitting: false };
  state.sendPasswordReset = vm.runInNewContext(`({${resetMethod}}).sendPasswordReset`, {
    window: { location: { origin: 'https://app.open-teleset.site' }, OPEN_TELESET_CONFIG: { pagesOrigin: 'https://open-teleset.site' } },
    _supabase: { auth: { resetPasswordForEmail: async (email, options) => { calls.push({ email, options }); return { error: null }; } } }
  });
  await state.sendPasswordReset();
  assert.equal(calls[0].options.redirectTo, 'https://open-teleset.site/dashboard');
  assert.equal(state.authSubmitting, false);
});
