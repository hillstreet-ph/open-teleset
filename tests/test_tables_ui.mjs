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

const computedEnd = html.indexOf('methods: {', dataEnd);
const computed = vm.runInNewContext(`({${html.slice(dataEnd, computedEnd).replace(/,\s*$/, '')}})`, {
  window: {}, localStorage: { getItem: () => null }, location: { search: '', hash: '' },
  APP_CONFIG: {}, Date, Math, JSON, URLSearchParams,
}).computed;

function methods(api = {}, extra = {}) {
  return vm.runInNewContext(
    `({${html.slice(html.indexOf('// Data tables'), html.indexOf('// Templates'))}})`,
    { _api: api, window: { confirm: () => true }, console, ...extra },
  );
}

const plain = (value) => JSON.parse(JSON.stringify(value));

function state() {
  return {
    tables: plain(data.call({}).tables),
    toasts: [],
    showToast(message, isError) { this.toasts.push({ message, isError }); },
    async loadTables() { this.loaded = (this.loaded || 0) + 1; },
  };
}

function withComputed(app) {
  for (const [key, getter] of Object.entries(computed)) {
    Object.defineProperty(app, key, { get: getter, configurable: true });
  }
  return app;
}

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
  assert.deepEqual(plain(config.tables.selected), []);
  assert.equal(config.tables.newPreset, '');
  assert.deepEqual(plain(config.tables.importForm), { open: false, format: 'auto', mode: 'append', content: '', filename: '' });
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
  for (const id of ['table-new-preset', 'table-import-open', 'table-import-format', 'table-import-mode',
    'table-import-file', 'table-import-content', 'table-import-run', 'table-export-csv', 'table-export-json',
    'table-filter', 'table-send-column', 'table-select-all', 'table-send-bulk', 'table-delete-selected']) {
    assert.ok(html.includes(`id="${id}"`), `missing #${id}`);
  }
  assert.ok(html.includes('data-testid="table-import"'));
});

test('creating a table posts the trimmed name and preset, then selects it', async () => {
  const calls = [];
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { table: { ...table(), name: body.name } } }; } };
  const app = { ...methods(api), ...state() };
  app.tables.newName = '  Suppliers ';
  app.tables.newPreset = 'contacts';
  await app.createTable();
  assert.deepEqual(plain(calls), [{ url: '/api/tables', body: { name: 'Suppliers', preset: 'contacts' } }]);
  assert.equal(app.tables.active.id, 't_1');
  assert.equal(app.tables.nameDraft, 'Suppliers');
  assert.equal(app.tables.newName, '');
  assert.equal(app.tables.newPreset, '');
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

test('choosing an import file loads its text and detects the format', async () => {
  const app = { ...methods(), ...state() };
  await app.readTableImportFile({ target: { files: [{ name: 'list.TSV', text: async () => 'a\tb\n' }] } });
  assert.equal(app.tables.importForm.content, 'a\tb\n');
  assert.equal(app.tables.importForm.filename, 'list.TSV');
  assert.equal(app.tables.importForm.format, 'tsv');
  await app.readTableImportFile({ target: { files: [{ name: 'rows.json', text: async () => '[]' }] } });
  assert.equal(app.tables.importForm.format, 'json');
});

test('importing rows posts the format and mode, then resets the import panel', async () => {
  const calls = [];
  const imported = { ...table(), rows: [{ id: 'r_1', cells: { c_1: 'old' } }, { id: 'r_2', cells: { c_1: 'new' } }] };
  const api = { post: async (url, body) => { calls.push({ url, body }); return { data: { table: imported, imported: 1, created_columns: ['c_3'] } }; } };
  const app = withComputed({ ...methods(api), ...state() });
  app.tables.active = table();
  app.tables.importForm = { open: true, format: 'csv', mode: 'append', content: 'Name\nnew\n', filename: '' };
  await app.importTableRows();
  assert.deepEqual(plain(calls), [{ url: '/api/tables/t_1/import', body: { format: 'csv', mode: 'append', content: 'Name\nnew\n' } }]);
  assert.equal(app.tables.active.rows.length, 2);
  assert.deepEqual(plain(app.tables.importForm), { open: false, format: 'auto', mode: 'append', content: '', filename: '' });
  assert.match(app.toasts.at(-1).message, /Imported 1 row and added 1 column/);
  assert.equal(app.loaded, 1);
  assert.equal(app.tables.busy, '');
});

test('blank import content never reaches the API', async () => {
  let calls = 0;
  const app = { ...methods({ post: async () => { calls++; } }), ...state() };
  app.tables.active = table();
  app.tables.importForm.content = '   ';
  await app.importTableRows();
  assert.equal(calls, 0);
});

test('exporting downloads the server file with its filename', async () => {
  const downloads = [];
  const api = {
    get: async (url, config) => {
      assert.equal(url, '/api/tables/t_1/export');
      assert.deepEqual(plain(config), { params: { format: 'csv' } });
      return { data: { content: 'Name\nold\n', filename: 'suppliers.csv', rows: 1 } };
    },
  };
  const app = {
    ...methods(api, {
      Blob: class { constructor(parts, options) { this.parts = parts; this.type = options?.type; } },
      URL: { createObjectURL: () => 'blob:export', revokeObjectURL: () => {} },
      document: {
        createElement: () => ({ click() { downloads.push(this.download); }, remove() {} }),
        body: { appendChild() {} },
      },
    }),
    ...state(),
  };
  app.tables.active = table();
  await app.exportTable('csv');
  assert.deepEqual(downloads, ['suppliers.csv']);
  assert.match(app.toasts.at(-1).message, /Exported 1 row as CSV/);
  assert.equal(app.tables.busy, '');
});

test('filtering and select-all operate on visible rows only', () => {
  const app = withComputed({ ...methods(), ...state() });
  app.tables.active = { ...table(), rows: [{ id: 'r_1', cells: { c_1: 'Alice' } }, { id: 'r_2', cells: { c_1: 'Bob' } }] };
  app.tables.filter = 'ali';
  assert.deepEqual(app.visibleTableRows.map(row => row.id), ['r_1']);
  app.toggleAllTableRows();
  assert.deepEqual(plain(app.tables.selected), ['r_1']);
  assert.equal(app.allVisibleRowsSelected, true);
  app.toggleTableRow({ id: 'r_2' });
  app.toggleAllTableRows();
  assert.deepEqual(plain(app.tables.selected), ['r_2'], 'clearing only touches visible rows');
  app.toggleTableRow({ id: 'r_2' });
  assert.deepEqual(plain(app.tables.selected), []);
});

test('sending selected rows prefills BulkSend and keeps consent unchecked', () => {
  const app = withComputed({
    ...methods(), ...state(),
    bulksend: { targets: '@Existing\n', messages: { draft: 'x' }, consentConfirmed: true },
    go(tab) { this.tab = tab; },
  });
  app.tables.active = {
    ...table(),
    rows: [{ id: 'r_1', cells: { c_1: '@Alice' } }, { id: 'r_2', cells: { c_1: 'bob' } }, { id: 'r_3', cells: { c_1: 'alice' } }],
  };
  app.tables.selected = ['r_1', 'r_2', 'r_3'];
  app.sendTableRowsToBulk();
  assert.equal(app.bulksend.targets, 'existing\nalice\nbob');
  assert.equal(app.bulksend.consentConfirmed, false, 'consent must never be pre-checked');
  assert.deepEqual(plain(app.bulksend.messages), {});
  assert.equal(app.tab, 'bulksend');
  assert.match(app.toasts.at(-1).message, /Consent is still unchecked/);
});

test('sending respects the chosen contact column and refuses empty values', () => {
  const app = withComputed({
    ...methods(), ...state(),
    bulksend: { targets: '', messages: {}, consentConfirmed: false },
    go(tab) { this.tab = tab; },
  });
  app.tables.active = {
    id: 't_1', name: 'Contacts',
    columns: [{ id: 'c_1', name: 'Name', type: 'text' }, { id: 'c_2', name: 'Username', type: 'text' }],
    rows: [{ id: 'r_1', cells: { c_1: 'Ada', c_2: '@ada' } }],
  };
  app.tables.selected = ['r_1'];
  app.tables.sendColumn = 'c_2';
  app.sendTableRowsToBulk();
  assert.equal(app.bulksend.targets, 'ada');

  app.tables.sendColumn = 'c_1';
  app.tables.active.rows = [{ id: 'r_2', cells: { c_1: '  ' } }];
  app.tables.selected = ['r_2'];
  app.tab = 'tables';
  app.sendTableRowsToBulk();
  assert.equal(app.tab, 'tables', 'no navigation on empty values');
  assert.equal(app.toasts.at(-1).isError, true);
});

test('deleting selected rows confirms and calls the row route per id', async () => {
  const calls = [];
  const api = { delete: async (url) => { calls.push(url); return { data: { table: { ...table(), rows: [] } } }; } };
  const app = withComputed({ ...methods(api), ...state() });
  app.tables.active = { ...table(), rows: [{ id: 'r_1', cells: {} }, { id: 'r_2', cells: {} }] };
  app.tables.selected = ['r_1', 'r_2'];
  await app.deleteSelectedRows();
  assert.deepEqual(calls, ['/api/tables/t_1/rows/r_1', '/api/tables/t_1/rows/r_2']);
  assert.deepEqual(plain(app.tables.selected), []);
  assert.equal(app.tables.active.rows.length, 0);
});

test('editing a column header patches its name and type', async () => {
  const calls = [];
  const api = { patch: async (url, body) => { calls.push({ url, body }); return { data: { table: table() } }; } };
  const app = { ...methods(api), ...state() };
  app.tables.active = table();
  const column = app.tables.active.columns[0];
  app.editTableColumn(column);
  assert.deepEqual(plain(app.tables.columnEdits[column.id]), { name: 'Name', type: 'text' });
  app.tables.columnEdits[column.id].name = ' Handle ';
  await app.saveTableColumn(column);
  assert.deepEqual(plain(calls), [{ url: '/api/tables/t_1/columns/c_1', body: { name: 'Handle', type: 'text' } }]);
  assert.deepEqual(plain(app.tables.columnEdits), {});
  assert.equal(app.loaded, 1);

  app.editTableColumn(column);
  app.cancelTableColumn(column);
  assert.deepEqual(plain(app.tables.columnEdits), {});
});
