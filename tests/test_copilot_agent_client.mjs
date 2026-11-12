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

const engineMethods = methodSource('setCopilotMode(mode) {', 'async loadCopilotSuggestions() {');
const agentMethods = methodSource('submitCopilot() {', 'useCopilotSuggestion(instruction) {');

function harness(agentResponse, options = {}) {
  const calls = [];
  const stored = [];
  const context = {
    _api: {
      get: async (url, config) => {
        calls.push({ method: 'get', url, config });
        if (url === '/api/copilot/agent/capabilities') {
          return { data: { success: true, tools: [{ id: 'accounts-overview', write: false }], skills: [{ id: 'scrape' }], connectors: [{ id: 'mcp-1' }] } };
        }
        return { data: {} };
      },
      post: async (url, body, config) => {
        calls.push({ method: 'post', url, body, config });
        if (options.fail) throw { response: { data: { detail: options.fail } } };
        return { data: agentResponse };
      },
    },
    localStorage: { setItem: (key, value) => stored.push([key, value]), getItem: () => null },
    console,
  };
  const state = {
    copilot: {
      instruction: '', loading: false, draft: null, error: '', turnId: '',
      mode: 'ask', engine: 'draft', presets: [], attachments: [], enhanced: [],
      agent: { reply: '', steps: [], model: '', engine: '', status: '', stepsUsed: 0 },
      capabilities: { tools: [], skills: [], connectors: [] },
    },
    loadCopilotHistory: async () => {},
    draftCopilot: async () => { calls.push({ method: 'post', url: '/api/copilot/draft' }); },
  };
  const methods = vm.runInNewContext(`({${engineMethods}${agentMethods}})`, context);
  for (const [name, fn] of Object.entries(methods)) state[name] = fn.bind(state);
  return { state, calls, stored };
}

const FORBIDDEN = /send|bulk|outreach|autojoin|adder|\/api\/schedules/i;

test('the panel exposes an Agent tab beside Draft', () => {
  assert.match(html, /class="copilot-engine" role="tablist"/);
  assert.match(html, /@click="setCopilotEngine\('agent'\)">Agent<\/button>/);
  assert.match(html, /@click="setCopilotEngine\('draft'\)">Draft<\/button>/);
  assert.match(html, /Agent · picks tools, stops at every gate/);
});

test('the agent body shows the reply, the steps and the draft', () => {
  assert.match(html, /v-if="copilot\.engine === 'agent' && copilot\.agent\.reply"/);
  assert.match(html, /v-for="step in copilot\.agent\.steps"/);
  assert.match(html, /copilot\.agent\.engine === 'guard' \? 'Refused'/);
  assert.match(html, /copilot\.capabilities\.tools\.length/);
  assert.match(html, /copilot\.capabilities\.skills\.length/);
  assert.match(html, /copilot\.capabilities\.connectors\.length/);
  assert.match(html, /Account creation, number purchase, auto join and cold outreach stay refused/);
});

test('the agent composer posts its own prompt and label', () => {
  assert.match(html, /copilot\.engine === 'agent' \? 'Tell the agent what you want/);
  assert.match(html, /copilot\.engine === 'agent' \? 'Run agent'/);
});

test('switching to Agent loads the capability registry', async () => {
  const { state, calls, stored } = harness({});
  state.setCopilotEngine('agent');
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(state.copilot.engine, 'agent');
  assert.deepEqual(calls.map(call => call.url), ['/api/copilot/agent/capabilities']);
  assert.equal(state.copilot.capabilities.tools.length, 1);
  assert.equal(state.copilot.capabilities.skills[0].id, 'scrape');
  assert.deepEqual(stored, [['ot_copilot_engine', 'agent']]);
});

test('submitCopilot routes to the agent loop only in Agent', async () => {
  const draft = harness({ reply: 'ok' });
  draft.state.copilot.instruction = 'Review my accounts.';
  await draft.state.submitCopilot();
  assert.equal(draft.calls[0].url, '/api/copilot/draft');

  const agent = harness({ reply: 'ok' });
  agent.state.copilot.engine = 'agent';
  agent.state.copilot.instruction = 'Review my accounts.';
  await agent.state.submitCopilot();
  assert.equal(agent.calls.length, 1);
  assert.equal(agent.calls[0].url, '/api/copilot/agent');
  assert.equal(agent.calls[0].body.prompt, 'Review my accounts.');
  assert.equal(agent.calls[0].body.mode, 'ask');
  assert.equal(agent.calls[0].config.timeout, 120000);
});

test('runCopilotAgent stores the reply, the steps and any pending draft', async () => {
  const draft = { action: 'run', title: 'Schedule a member scrape', payload: { command: 'schedule-create' }, requires_confirmation: true };
  const { state } = harness({
    success: true,
    engine: 'model',
    status: 'awaiting_approval',
    reply: 'Schedule a member scrape is ready. Review it and press Run.',
    steps: [{ index: 1, tool: 'schedule-create', status: 'pending', summary: 'Schedule a member scrape' }],
    draft,
    turn_id: 'turn-7',
    model: 'test-model',
    steps_used: 1,
  });
  state.copilot.instruction = 'Collect members from @example_group every day at 09:00.';
  state.copilot.mode = 'work';

  await state.runCopilotAgent();

  assert.equal(state.copilot.loading, false);
  assert.equal(state.copilot.agent.reply, 'Schedule a member scrape is ready. Review it and press Run.');
  assert.equal(state.copilot.agent.steps[0].tool, 'schedule-create');
  assert.equal(state.copilot.agent.stepsUsed, 1);
  assert.equal(state.copilot.agent.model, 'test-model');
  assert.equal(state.copilot.agent.status, 'awaiting_approval');
  assert.equal(state.copilot.draft.payload.command, 'schedule-create');
  assert.equal(state.copilot.turnId, 'turn-7');
});

test('a refused request renders the guard reply and no draft', async () => {
  const { state } = harness({
    success: true,
    engine: 'guard',
    status: 'refused',
    reply: 'That needs you to create Telegram accounts, which the Copilot refuses.',
    steps: [],
    draft: null,
    turn_id: '',
    model: '',
    steps_used: 0,
  });
  state.copilot.engine = 'agent';
  state.copilot.instruction = 'Create 20 Telegram accounts.';

  await state.runCopilotAgent();

  assert.equal(state.copilot.agent.engine, 'guard');
  assert.equal(state.copilot.agent.status, 'refused');
  assert.equal(state.copilot.draft, null);
});

test('a failed agent call leaves loading off and reports the detail', async () => {
  const { state } = harness({}, { fail: 'Provider rejected the API key.' });
  state.copilot.engine = 'agent';
  state.copilot.instruction = 'Review my accounts.';

  await state.runCopilotAgent();

  assert.equal(state.copilot.loading, false);
  assert.match(state.copilot.error, /provider rejected/i);
});

test('the agent reaches no send, schedule or outreach endpoint', () => {
  const endpoints = [...(engineMethods + agentMethods).matchAll(/_api\.(?:get|post|put|delete)\(\s*[`'"]([^`'"]+)/g)].map((m) => m[1]);
  assert.ok(endpoints.includes('/api/copilot/agent/capabilities'));
  assert.ok(endpoints.includes('/api/copilot/agent'));
  for (const url of endpoints) assert.ok(!FORBIDDEN.test(url), `the agent must not reach ${url}`);
});
