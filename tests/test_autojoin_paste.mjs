import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync('static/dashboard.html', 'utf8');
const start = html.indexOf('autojoinLinks() {');
const end = html.indexOf('autojoinValidLinks() {');
const { autojoinLinks } = vm.runInNewContext(`({${html.slice(start, end)}})`);

// Copy into this realm so strict deep-equality compares values, not prototypes.
const links = (urls) => Array.from(autojoinLinks.call({ autojoin: { urls } }));

test('links are found inside a pasted block of text', () => {
  const pasted = 'Join these groups: https://t.me/s5philippines and t.me/+AbCdEf123456. '
    + 'The second one needs approval.';
  assert.deepEqual(links(pasted), ['https://t.me/s5philippines', 't.me/+AbCdEf123456']);
});

test('punctuation after a pasted link is not part of the link', () => {
  assert.deepEqual(links('see (https://t.me/durov), thanks'), ['https://t.me/durov']);
});

test('one-entry-per-line input still works', () => {
  assert.deepEqual(links('https://t.me/chanone\n@chantwo\n+AbCdEf123456'), [
    'https://t.me/chanone', '@chantwo', '+AbCdEf123456',
  ]);
});

test('duplicates across the paste collapse to one entry', () => {
  assert.deepEqual(links('https://t.me/durov\nagain: https://t.me/durov'), ['https://t.me/durov']);
});

test('an empty box yields no links', () => {
  assert.deepEqual(links('   \n\n '), []);
});

test('telegram.me and telegram.dog links are picked up from prose', () => {
  assert.deepEqual(links('a telegram.me/durov b telegram.dog/+XyZ12345'), [
    'telegram.me/durov', 'telegram.dog/+XyZ12345',
  ]);
});
