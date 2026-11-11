import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');

function slice(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  assert.ok(start >= 0, `missing ${startMarker}`);
  const end = html.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `missing ${endMarker} after ${startMarker}`);
  return html.slice(start, end);
}

function methods(src) {
  return vm.runInNewContext(`({${src}})`, {});
}

const component = vm.runInNewContext(`(${slice('const JobHistory = {', 'const app = createApp({').replace('const JobHistory = ', '').trim()})`, {});

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('the dashboard application script still parses', () => {
  const start = html.indexOf('<script>', html.indexOf('supabase.umd.js'));
  const end = html.indexOf('</script>', start);
  assert.ok(start >= 0 && end > start, 'main application script not found');
  assert.doesNotThrow(() => new vm.Script(html.slice(start + '<script>'.length, end)));
});

test('both progress trackers and the schedule card render the table component', () => {
  assert.ok(component, 'JobHistory must be a plain object literal');
  assert.match(html, /app\.component\('job-history', JobHistory\)/);
  const uses = html.match(/<job-history\b/g) || [];
  assert.ok(uses.length >= 3, `expected the tracker, the tab tracker and the schedule card, found ${uses.length}`);
  assert.ok(!html.includes('Link history'), 'the old bullet-list link history must be gone');
  assert.ok(!html.includes('Recipient history'), 'the old bullet-list recipient history must be gone');
});

test('the table names every column the operator asked for', () => {
  const template = component.template;
  for (const heading of ['Account', 'Result', 'Detail', 'Time']) {
    assert.ok(template.includes(`<th>${heading}</th>`), `missing column ${heading}`);
  }
  assert.match(template, /<th>\{\{ noun \}\}<\/th>/);
  assert.match(template, /row\.updated/, 'each row must show its own timestamp');
  assert.match(template, /row\.target/, 'each row must show the group or channel link');
  assert.match(template, /account\(row\.account_id\)/, 'each row must name the account that acted');
});

test('rows are newest first and honest about the noun and the account', () => {
  const ctx = {
    job: {
      kind: 'auto_join',
      results: [
        { account_id: 'a', target: 'https://t.me/one', state: 'completed', reason: '', updated: 100 },
        { account_id: 'b', target: 'https://t.me/two', state: 'failed', reason: 'account_banned', updated: 200 },
      ],
    },
    expanded: false,
    limit: 12,
  };
  assert.equal(component.computed.noun.call(ctx), 'Group or channel');
  assert.deepEqual(component.computed.rows.call(ctx).map(row => row.target), ['https://t.me/two', 'https://t.me/one']);
  ctx.rows = component.computed.rows.call(ctx);
  assert.equal(component.computed.visible.call(ctx).length, 2);
  ctx.job.kind = 'personal_send';
  assert.equal(component.computed.noun.call(ctx), 'Recipient');
});

test('a restricted account is named with a plain-language reason', () => {
  const ctx = { job: { kind: 'auto_join', results: [], restricted_accounts: { a: 'account_banned' } } };
  assert.deepEqual(plain(component.computed.restricted.call(ctx)), [['a', 'account_banned']]);
  assert.match(component.template, /cannot be lifted from this dashboard/);
});

test('result badges separate joined, failed and unfinished rows', () => {
  assert.equal(component.methods.resultClass('completed'), 'badge-success');
  assert.equal(component.methods.resultClass('failed'), 'badge-danger');
  assert.equal(component.methods.resultClass('pending'), 'badge-info');
  assert.equal(component.methods.resultClass('skipped'), 'badge-normal');
});

test('ban and revocation reasons are explained instead of shown as class names', () => {
  const { outreachReason } = methods(slice('outreachReason(reason) {', 'parseAdderRecipients() {'));
  assert.match(outreachReason('account_banned'), /banned by Telegram/);
  assert.match(outreachReason('session_revoked'), /signed out/);
  assert.match(outreachReason('account_frozen'), /frozen/);
  assert.match(outreachReason('channel_limit_reached'), /channel limit/);
  assert.match(outreachReason('not_attempted'), /Not attempted/);
  assert.match(outreachReason('invalid_link'), /not a valid/i);
  assert.match(outreachReason('already_member'), /Already a member/);
  assert.match(outreachReason('SomeNewError'), /Telegram refused this target \(SomeNewError\)/);
  assert.equal(outreachReason('something_else'), 'something else');
});

test('API validation arrays are flattened instead of showing [object Object]', () => {
  const { apiErrorText } = methods(slice('apiErrorText(error, fallback) {', 'hookStamp(timestamp) {'));
  assert.equal(apiErrorText({ response: { data: { detail: 'Plain message' } } }, 'fallback'), 'Plain message');
  const arrayError = { response: { data: { detail: [
    { loc: ['body', 'urls'], msg: 'Enter at least one link' },
    { loc: ['body', 'delay'], msg: 'Delay is too small' },
  ] } }, message: 'Request failed' };
  assert.equal(apiErrorText(arrayError, 'fallback'), 'urls: Enter at least one link; delay: Delay is too small');
  assert.equal(apiErrorText({ message: 'Network down' }, 'fallback'), 'Network down');
  assert.equal(apiErrorText({}, 'fallback'), 'fallback');
});

test('a scheduled Auto Join record becomes the same table shape', () => {
  const { scheduleJoinJob } = methods(slice('scheduleJoinJob(sch) {', 'getScheduleDescription(sch) {'));
  const job = scheduleJoinJob({ last_join_result: {
    account: 'acct',
    stopped_reason: 'account_banned',
    targets: [
      { target: 'https://t.me/+InviteHash12', state: 'failed', reason: 'account_banned', at: 1000 },
      { target: 'https://t.me/one', state: 'joined', reason: '', at: 990 },
      { target: 'https://t.me/two', state: 'pending', reason: 'not_attempted', at: null },
    ],
  } });
  assert.equal(job.kind, 'auto_join');
  assert.deepEqual(job.results.map(row => row.state), ['failed', 'completed', 'skipped']);
  assert.equal(job.results[0].account_id, 'acct');
  assert.equal(job.results[2].updated, 0);
  assert.deepEqual(plain(job.restricted_accounts), { acct: 'account_banned' });
  assert.equal(scheduleJoinJob({}), null);
  assert.deepEqual(plain(scheduleJoinJob({ last_join_result: { account: 'acct', stopped_reason: 'telegram_cooldown', targets: [] } }).restricted_accounts), {});
});

test('a rotated Auto Join record names the account behind every row', () => {
  const { scheduleJoinJob } = methods(slice('scheduleJoinJob(sch) {', 'getScheduleDescription(sch) {'));
  const job = scheduleJoinJob({ last_join_result: {
    account: 'b',
    accounts: ['a', 'b'],
    stopped_reason: 'account_banned',
    targets: [
      { target: 'https://t.me/one', state: 'joined', reason: '', account: 'a', at: 1000 },
      { target: 'https://t.me/two', state: 'failed', reason: 'account_banned', account: 'a', at: 1010 },
      { target: 'https://t.me/three', state: 'joined', reason: '', account: 'b', at: 1020 },
    ],
  } });
  assert.deepEqual(job.results.map(row => row.account_id), ['a', 'a', 'b']);
  assert.deepEqual(job.results.map(row => row.state), ['completed', 'failed', 'completed']);
  assert.deepEqual(plain(job.restricted_accounts), { a: 'account_banned' });
});
