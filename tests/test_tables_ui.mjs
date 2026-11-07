// Data tables: the dashboard half of the spreadsheet-style list feature.
// The API contract is covered in tests/test_tables.py; here we prove the panel
// state starts clean, and that every control calls the route the backend serves.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const appStart = html.indexOf('const app = createApp({');
const dataStart = html.indexOf('data() {', appStart);
const dataEnd = html.indexOf('computed: {', dataStart);
const data = vm.runInNewContext(`({${html.slice(dataStart, dataEnd).replace(/,\s*$/, '')}})`, {
  window: {}, localStorage: { getItem: () => null }, location: { search: '', hash: '' },
  APP_CONFIG: {}, Date, Math, JSON, URLSearchParams,
}).data;

function methods(api = {}, extra = {}) {
  return vm.runInNewContext(
    `({${html.slice(html.indexOf('// Data tables'), html.indexOf('// Templates'))}})`,
    { _api: api, window: { confirm: () => true }, console, ...extra },
  );
}

function state() {
  return {
    tables: {
      loading: false, busy: '', error: '', list: [], capacity: { used: 0, limit: 30 },
      active: null, nameDraft: '', newName: '', columnForm: { name: '', type: 'text' },
    },
    toasts: [],
    showToast(message, isError) { this.toasts.push({ message, isError }); },
    async loadTables() { this.loaded = (this.loaded || 0) + 1; },
  };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

const table = () => ({
  id: 't_1', name: 'Suppliers',
  columns: [{ id: 'c_1', name: 'Name', type: 'text' }, { id: 'c_2', name: 'Score', type: 'number' }],
  rows: [{ id: 'r_1', cells: { c_1: 'old' } }],
});

test('data() initialises the data-table panel', () => {
  const config = data.call({});
  assert.ok(config.tables, 'tables state missing');
  assert.deepEqual(plain(config.tables.list), []);
  assert.equal(config.tables.capacity.limit, 30);
  assert.equal(config.tables.active, null);
  assert.deepEqual(plain(config.tables.columnForm), { name: '', type: 'text' });
});

test('the sidebar, tab panel, gate and sheet are wired into the dashboard', () => {
  assert.ok(html.includes("{ tab: 'tables', label: 'Data Tables', icon: 'table' }"));
  assert.ok(html.includes("v-show=\"tab === 'tables'\""));
  assert.ok(html.includes('id="gate-tables"'));
  assert.ok(html.includes('data-testid="table-sheet"'));
  assert.ok(html.includes("if (tab === 'tables') this.loadTables()"));
  assert.ok(html.includes("if (this.tab === 'tables') this.loadTables()"));
  for (const type of ['text', 'number', 'date', 'checkbox']) {
    assert.ok(html.includes(`<option value="${type}">`), `missing column type ${type}`);
  }
});

test('creating a table posts the trimmed name and selects it', async () => {
  const calls = [];
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { table: { ...table(), name: body.name } } }; } };
  const app = { ...methods(api), ...state() };
  app.tables.newName = '  Suppliers ';
  await app.createTable();
  assert.deepEqual(plain(calls), [{ url: '/api/tables', body: { name: 'Suppliers' } }]);
  assert.equal(app.tables.active.id, 't_1');
  assert.equal(app.tables.nameDraft, 'Suppliers');
  assert.equal(app.tables.newName, '');
  assert.equal(app.loaded, 1);
});

test('opening a table loads its full sheet and clears a previous error', async () => {
  const api = { get: async (url) => { assert.equal(url, '/api/tables/t_1'); return { data: { table: table() } }; } };
  const app = { ...methods(api), ...state() };
  app.tables.error = 'old failure';
  await app.openTable('t_1');
  assert.equal(app.tables.active.rows[0].cells.c_1, 'old');
  assert.equal(app.tables.error, '');
});

test('editing a cell patches only that row and column', async () => {
  const calls = [];
  const updated = { ...table(), rows: [{ id: 'r_1', cells: { c_1: 'new' } }] };
  const api = { patch: async (url, body) => { calls.push({ url, body }); return { data: { table: updated } }; } };
  const app = { ...methods(api), ...state() };
  app.tables.active = table();
  await app.setTableCell(app.tables.active.rows[0], app.tables.active.columns[0], 'new');
  assert.deepEqual(plain(calls), [{ url: '/api/tables/t_1/rows/r_1', body: { cells: { c_1: 'new' } } }]);
  assert.equal(app.tables.active.rows[0].cells.c_1, 'new');
  assert.equal(app.tables.busy, '');
});

test('a rejected cell shows the server detail and reverts the sheet', async () => {
  const api = {
    patch: async () => { const error = new Error('bad'); error.response = { data: { detail: 'Data tables hold working data only.' } }; throw error; },
    get: async () => ({ data: { table: table() } }),
  };
  const app = { ...methods(api), ...state() };
  app.tables.active = table();
  await app.setTableCell(app.tables.active.rows[0], app.tables.active.columns[0], 'A'.repeat(512));
  assert.equal(app.tables.error, 'Data tables hold working data only.');
  assert.equal(app.tables.active.rows[0].cells.c_1, 'old');
  assert.equal(app.tables.busy, '');
  assert.equal(app.toasts.at(-1).isError, true);
});

test('adding a column posts its name and type, then resets the form', async () => {
  const calls = [];
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { table: table() } }; } };
  const app = { ...methods(api), ...state() };
  app.tables.active = table();
  app.tables.columnForm = { name: ' Due date ', type: 'date' };
  await app.addTableColumn();
  assert.deepEqual(plain(calls), [{ url: '/api/tables/t_1/columns', body: { name: 'Due date', type: 'date' } }]);
  assert.deepEqual(plain(app.tables.columnForm), { name: '', type: 'text' });
  assert.equal(app.loaded, 1);
});

test('adding and deleting rows calls the row routes', async () => {
  const calls = [];
  const api = {
    post: async (url, body) => { calls.push({ method: 'POST', url, body }); return { data: { table: table() } }; },
    delete: async (url) => { calls.push({ method: 'DELETE', url }); return { data: { table: { ...table(), rows: [] } } }; },
  };
  const app = { ...methods(api), ...state() };
  app.tables.active = table();
  await app.addTableRow();
  await app.deleteTableRow({ id: 'r_1' });
  assert.deepEqual(plain(calls), [
    { method: 'POST', url: '/api/tables/t_1/rows', body: { cells: {} } },
    { method: 'DELETE', url: '/api/tables/t_1/rows/r_1' },
  ]);
  assert.equal(app.tables.active.rows.length, 0);
});

test('renaming and deleting a table are confirmed and reflected in the panel', async () => {
  const calls = [];
  const api = {
    patch: async (url, body) => { calls.push({ method: 'PATCH', url, body }); return { data: { table: { ...table(), name: body.name } } }; },
    delete: async (url) => { calls.push({ method: 'DELETE', url }); return { data: { success: true } }; },
  };
  const app = { ...methods(api), ...state() };
  app.tables.active = table();
  app.tables.nameDraft = 'Vendors';
  await app.renameTable();
  assert.deepEqual(plain(calls[0]), { method: 'PATCH', url: '/api/tables/t_1', body: { name: 'Vendors' } });
  assert.equal(app.tables.active.name, 'Vendors');

  await app.deleteTable();
  assert.deepEqual(plain(calls[1]), { method: 'DELETE', url: '/api/tables/t_1' });
  assert.equal(app.tables.active, null);
  assert.equal(app.tables.nameDraft, '');
});

test('cancelling the delete confirmation leaves the table alone', async () => {
  let calls = 0;
  const app = { ...methods({ delete: async () => { calls++; } }, { window: { confirm: () => false } }), ...state() };
  app.tables.active = table();
  await app.deleteTable();
  assert.equal(calls, 0);
  assert.equal(app.tables.active.id, 't_1');
});
