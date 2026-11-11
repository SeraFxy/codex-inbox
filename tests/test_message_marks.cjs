const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'board.html'), 'utf8');
const source = html.slice(html.indexOf('function markSpans('), html.indexOf('function paintMarks('));
const ctx = vm.createContext({});
vm.runInContext(source, ctx);
const plain = x => JSON.parse(JSON.stringify(x));   // vm 里建的数组原型不同，比较前转成本 realm 的

test('saved positions are used while the text still matches', () => {
  const spans = ctx.markSpans('alpha beta gamma', [{ start: 11, end: 16, text: 'gamma' }, { start: 0, end: 5, text: 'alpha' }]);
  assert.deepEqual(plain(spans.map(s => [s.start, s.end, s.i])), [[0, 5, 1], [11, 16, 0]]);
});

test('marks follow their text when the rendering shifts and drop when the text is gone', () => {
  const spans = ctx.markSpans('new intro alpha beta', [{ start: 0, end: 5, text: 'alpha' }, { start: 6, end: 10, text: 'zeta' }]);
  assert.deepEqual(plain(spans.map(s => [s.start, s.end, s.i])), [[10, 15, 0]]);
  assert.deepEqual(plain(ctx.markSpans('anything', undefined)), []);
});
