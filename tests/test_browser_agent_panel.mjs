// The panel's half of the browser agent: what it asks for, what it refuses to send,
// and which step it lets the operator press. The methods run against a stubbed _api, so
// a mistake in the approve-then-run flow fails here rather than in an operator's browser.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

// Objects built inside the vm sandbox come from a different realm's prototype, which
// strict deep-equality rejects; compare their plain values instead.
const plain = (value) => JSON.parse(JSON.stringify(value));

function panel({ responses = {}, failures = {} } = {}) {
  const calls = []
  const respond = (method, url, body) => {
    calls.push({ method, url, body })
    if (failures[url]) {
      const error = new Error('refused')
      error.response = { data: { detail: failures[url] } }
      return Promise.reject(error)
    }
    return Promise.resolve({ data: responses[url] ?? { success: true } })
  }
  const sandbox = {
    _api: {
      get: (url) => respond('GET', url, undefined),
      post: (url, body) => respond('POST', url, body),
      delete: (url) => respond('DELETE', url, undefined),
    },
    JSON,
    Number,
    window: { setTimeout: () => { throw new Error('a plan step must never schedule itself') } },
  }
  const start = html.indexOf('// ---- AI browser plans')
  const end = html.indexOf('// ---- AI providers ----', start)
  assert.ok(start > 0 && end > start, 'plan methods not found')
  const methods = vm.runInNewContext(`({${html.slice(start, end).replace(/,\s*$/, '')}})`, sandbox)
  const toasts = []
  const state = {
    cloudBrowser: {
      busy: '',
      selected: 't1',
      plan: { id: '', goal: '', page: '', steps: [], next: null, done: false, seconds_left: 0, text: '', error: '' },
    },
    toasts,
    showToast(message, isError) { toasts.push({ message, isError }) },
    ...methods,
  }
  return { state, calls, toasts }
}

const step = (index, over) => ({
  index, action: 'key', label: 'Enter', why: '', status: 'todo', result: '',
  needs_text: false, key: 'Enter', url: '', ...over,
})

const drafted = {
  id: 'p-1', goal: 'sign in', page: { url: 'https://web.telegram.org/a/', title: 'Telegram' },
  target_id: 't1', done: false, seconds_left: 890,
  steps: [step(0, { action: 'type', label: 'Phone number', key: '', needs_text: true }), step(1)],
}

test('a goal is asked as a plan, and no step is run with it', async () => {
  const { state, calls } = panel({ responses: { '/api/cloud-browser/plans': { success: true, plan: drafted } } })
  state.cloudBrowser.plan.goal = '  sign in  '

  await state.proposeCloudBrowserPlan()

  assert.deepEqual(plain(calls), [{
    method: 'POST', url: '/api/cloud-browser/plans', body: { target_id: 't1', goal: 'sign in' },
  }])
  assert.equal(state.cloudBrowser.plan.id, 'p-1')
  assert.equal(state.cloudBrowser.plan.steps.length, 2)
  assert.equal(state.cloudBrowser.busy, '')
  assert.match(state.toasts[0].message, /nothing has run yet/)
})

test('an empty goal asks for nothing', async () => {
  const { state, calls } = panel()
  state.cloudBrowser.plan.goal = '   '

  await state.proposeCloudBrowserPlan()

  assert.deepEqual(calls, [])
  assert.equal(state.cloudBrowser.busy, '')
})

test('a page with no page selected asks for nothing', async () => {
  const { state, calls } = panel()
  state.cloudBrowser.selected = ''
  state.cloudBrowser.plan.goal = 'sign in'

  await state.proposeCloudBrowserPlan()

  assert.deepEqual(calls, [])
})

test('the first step waiting is the only one that can run', () => {
  const { state } = panel()
  state.applyCloudPlan(drafted)

  assert.equal(state.cloudBrowser.plan.next, 0)
  assert.equal(state.cloudBrowser.plan.done, false)
  assert.equal(state.cloudBrowser.plan.page, 'https://web.telegram.org/a/')
  assert.equal(state.cloudBrowser.plan.seconds_left, 890)

  state.applyCloudPlan({ ...drafted, steps: [step(0, { status: 'done', result: 'Typed 9 characters' }), step(1)] })
  assert.equal(state.cloudBrowser.plan.next, 1)
})

test('a finished plan has nothing left to press', () => {
  const { state } = panel()
  state.applyCloudPlan({
    ...drafted,
    done: true,
    steps: [step(0, { status: 'done', result: 'Sent Enter', key: 'Enter', needs_text: false }), step(1, { status: 'done', result: 'Sent Enter' })],
  })

  assert.equal(state.cloudBrowser.plan.next, null)
  assert.equal(state.cloudBrowser.plan.done, true)
})

test('an empty plan is not finished', () => {
  const { state } = panel()
  state.applyCloudPlan({ id: 'p-2', page: { url: 'https://x/' }, steps: [] })

  assert.equal(state.cloudBrowser.plan.done, false)
  assert.equal(state.cloudBrowser.plan.next, null)
})

test('the text of a type step is sent once and then forgotten', async () => {
  const { state, calls } = panel({
    responses: {
      '/api/cloud-browser/plans/p-1/steps/0/run': {
        success: true,
        plan: { ...drafted, steps: [step(0, { action: 'type', label: 'Phone number', needs_text: true, status: 'done', result: 'Typed 13 characters' }), step(1)] },
      },
    },
  })
  state.applyCloudPlan(drafted)
  state.cloudBrowser.plan.text = '+639170000111'

  await state.runCloudBrowserStep(state.cloudBrowser.plan.steps[0])

  assert.deepEqual(plain(calls), [{
    method: 'POST', url: '/api/cloud-browser/plans/p-1/steps/0/run', body: { text: '+639170000111' },
  }])
  // the value is not kept in the page's memory after the step has run
  assert.equal(state.cloudBrowser.plan.text, '')
  assert.ok(!JSON.stringify(state.cloudBrowser.plan.steps).includes('+639170000111'))
  assert.equal(state.cloudBrowser.plan.next, 1)
  assert.match(state.toasts[0].message, /check the live view/)
})

test('a key step sends no text at all', async () => {
  const { state, calls } = panel({ responses: { '/api/cloud-browser/plans/p-1/steps/1/run': { success: true, plan: drafted } } })
  state.applyCloudPlan(drafted)
  state.cloudBrowser.plan.text = 'leftover'

  await state.runCloudBrowserStep({ index: 1, action: 'key', needs_text: false })

  assert.equal(plain(calls[0].body).text, '')
})

test('a step that did not run leaves the plan where it was', async () => {
  const { state, calls } = panel({ failures: { '/api/cloud-browser/plans/p-1/steps/0/run': 'That control is no longer on the page. Ask for a new plan.' } })
  state.applyCloudPlan(drafted)
  state.cloudBrowser.plan.text = '+63'

  await state.runCloudBrowserStep(state.cloudBrowser.plan.steps[0])

  assert.equal(state.cloudBrowser.plan.error, 'That control is no longer on the page. Ask for a new plan.')
  assert.equal(state.cloudBrowser.plan.next, 0)
  assert.equal(state.cloudBrowser.busy, '')
  assert.equal(calls.length, 1)
})

test('a provider that refuses is said in the card, not as a toast', async () => {
  const { state } = panel({ failures: { '/api/cloud-browser/plans': 'Configure an AI provider and API key in Settings → AI providers.' } })
  state.cloudBrowser.plan.goal = 'sign in'

  await state.proposeCloudBrowserPlan()

  assert.equal(state.cloudBrowser.plan.error, 'Configure an AI provider and API key in Settings → AI providers.')
  assert.deepEqual(state.toasts, [])
  assert.equal(state.cloudBrowser.plan.steps.length, 0)
})

test('discarding takes the plan away but keeps the goal', async () => {
  const { state, calls } = panel()
  state.cloudBrowser.plan.goal = 'sign in'
  state.applyCloudPlan(drafted)

  await state.discardCloudBrowserPlan()

  assert.deepEqual(plain(calls), [{ method: 'DELETE', url: '/api/cloud-browser/plans/p-1' }])
  assert.equal(state.cloudBrowser.plan.id, '')
  assert.equal(state.cloudBrowser.plan.steps.length, 0)
  assert.equal(state.cloudBrowser.plan.goal, 'sign in')
  assert.equal(state.cloudBrowser.busy, '')
})

test('each action says what it will do before it is approved', () => {
  const { state } = panel()

  assert.equal(state.planStepText(step(0, { action: 'open', url: 'https://my.telegram.org' })), 'Open https://my.telegram.org')
  assert.equal(state.planStepText(step(0, { action: 'key', key: 'Tab' })), 'Press Tab')
  assert.equal(state.planStepText(step(0, { action: 'type', label: 'Phone number' })), 'Type into Phone number')
  assert.equal(state.planStepText(step(0, { action: 'click', label: 'Sign in' })), 'Click Sign in')
})

test('the card keeps the operator in the loop', () => {
  const card = html.slice(html.indexOf('<h3>AI plan</h3>'), html.indexOf('<!-- Proxies -->'))

  assert.match(card, /type="password"/, 'the text a step types is masked while it is entered')
  assert.equal(card.split('runCloudBrowserStep').length - 1, 1, 'a step runs only from its own button')
  assert.match(card, /@click="runCloudBrowserStep\(step\)"/)
  assert.match(card, /v-if="step\.status === 'todo' && step\.index === cloudBrowser\.plan\.next"/, 'only the next step gets a run button')
  assert.match(card, /Never write a password in the goal/)
  assert.match(card, /nothing auto-run/)
  assert.doesNotMatch(card, /setInterval|setTimeout|autostart/, 'nothing on the card runs a step by itself')
})
