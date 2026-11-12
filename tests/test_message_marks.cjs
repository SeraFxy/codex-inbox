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

test('bold and highlight can overlap, and removing one style preserves the other', () => {
  const highlight = { start: 0, end: 10, text: '0123456789', style: 'highlight' };
  const selection = { start: 3, end: 7, text: '3456' };
  const bold = { ...selection, style: 'bold' };
  const combined = plain(ctx.editMarkRanges([highlight], selection, 'bold', false));
  assert.deepEqual(combined, [bold, highlight]);
  assert.deepEqual(plain(ctx.editMarkRanges(combined, selection, 'highlight', true)), [
    bold,
    { start: 0, end: 3, text: '012', style: 'highlight' },
    { start: 7, end: 10, text: '789', style: 'highlight' },
  ]);
});

test('a clipped search preview paints only the visible part of a mark at its original offset', () => {
  const mark = { start: 3, end: 8, text: '34567', style: 'highlight' };
  const [span] = ctx.markSpans('56789', [mark], 5);
  assert.deepEqual(plain([span.start, span.end]), [0, 3]);
  assert.deepEqual(plain(ctx.markSpans('5abc9', [mark], 5)), []);
});

test('overlapping or adjacent selections of the same style become one contiguous mark', () => {
  const marks = [{ start: 0, end: 4, text: '0123', style: 'bold' }, { start: 7, end: 9, text: '78', style: 'bold' }];
  assert.deepEqual(plain(ctx.editMarkRanges(marks, { start: 2, end: 7, text: '23456' }, 'bold', false)), [
    { start: 0, end: 9, text: '012345678', style: 'bold' },
  ]);
});
