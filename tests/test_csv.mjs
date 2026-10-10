import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const context = {};
vm.runInNewContext(readFileSync('static/csv.js', 'utf8'), context);
const csv = context.OpenTelesetCSV;
test('CSV supports BOM, CRLF, commas, escaped quotes and multiline messages', () => {
    const parsed = csv.recipients('\uFEFFrecipient,message\r\n@Alpha,"Hello, ""friend""\nSecond line"\r\n222,Goodbye\r\n', 20);
    assert.deepEqual(Array.from(parsed.targets), ['alpha', '222']);
    assert.equal(parsed.messages.alpha, 'Hello, "friend"\nSecond line');
    assert.equal(parsed.messages['222'], 'Goodbye');
});
test('scraper export imports username with numeric ID fallback', () => {
    const result = csv.recipients(csv.encode([['username', 'id', 'first_name'], ['alpha', '111', 'A'], ['', '222', 'B']]), 20);
    assert.deepEqual(Array.from(result.targets), ['alpha', '222']);
});
test('reject malformed, oversized, invalid and conflicting CSV without partial imports', () => {
    for (const input of ['recipient,message\nalpha,"open', 'recipient,message\nalpha,"text"tail', 'recipient,message\nalpha,x,y', 'recipient,recipient\nalpha,beta', 'recipient\n../path', 'recipient,message\nalpha,first\n@ALPHA,second', 'recipient\n', 'x'.repeat(2 * 1024 * 1024 + 1)]) assert.throws(() => csv.recipients(input, 20));
    assert.throws(() => csv.recipients('recipient\n111\n222', 1));
});
test('deduplication, numeric IDs and message-only files preserve contents', () => {
    const parsed = csv.recipients('recipient,message\nalpha,hello\n@ALPHA,hello\n9007199254740999,hi', 20);
    assert.equal(parsed.targets.length, 2);
    assert.equal(parsed.targets[1], '9007199254740999');
    assert.equal(csv.recipients('message\n"hello, world"', 20).message, 'hello, world');
});
test('CSV export neutralizes spreadsheet formulas and escapes quotes', () => {
    const result = csv.parse(csv.encode([['=SUM(A1)', '+cmd', '-42', '@evil', '\t=cmd', 'He said "Hi"', 'a,b\nc']]));
    assert.deepEqual(Array.from(result[0]), ["'=SUM(A1)", "'+cmd", "'-42", "'@evil", "'\t=cmd", 'He said "Hi"', 'a,b\nc']);
});
