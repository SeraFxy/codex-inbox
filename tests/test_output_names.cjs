const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'board.html'), 'utf8');
const source = html.slice(html.indexOf('function outEdit('), html.indexOf('function toggleOutFav('));

function setup(reply = { ok: true, message: 'Saved' }) {
  const calls = [], input = { value: 'My material', focus() {} };
  const ctx = vm.createContext({
    calls, tagEdit: { kind: 'output', key: 'https://example.com/report' }, dOut: { demo: { version: 10 } },
    oLoaded: true, tab: 'board', oq: '',
    document: { getElementById: () => input },
    toast: text => calls.push(['toast', text]),
    post: async (...args) => { calls.push(['post', ...args]); return reply; },
    closeTag: () => { ctx.tagEdit = null; calls.push(['close']); },
    load: async () => calls.push(['load']), searchOut: async () => calls.push(['search'])
  });
  vm.runInContext(source, ctx);
  return { ctx, calls, input, run: code => vm.runInContext(code, ctx) };
}

test('save persists the displayed name and invalidates expanded result caches', async () => {
  const s = setup();
  await s.run('saveOutputName(false)');
  assert.equal(s.calls.find(x => x[0] === 'post')[1], 'out_name');
  assert.equal(s.calls.find(x => x[0] === 'post')[3].title, 'My material');
  assert.equal(s.ctx.dOut.demo.version, -1);
  assert.equal(s.ctx.tagEdit, null);
  assert.ok(s.calls.some(x => x[0] === 'load'));
});

test('failed save leaves the editor and typed value intact for retry', async () => {
  const s = setup({ ok: false, message: 'Try again' });
  await s.run('saveOutputName(false)');
  assert.equal(s.ctx.tagEdit.kind, 'output');
  assert.equal(s.ctx.tagEdit.saving, false);
  assert.equal(s.input.value, 'My material');
  assert.ok(!s.calls.some(x => x[0] === 'close'));
});

test('blank input cannot accidentally erase a name; reset is a separate explicit action', async () => {
  const s = setup();
  s.input.value = '   ';
  await s.run('saveOutputName(false)');
  assert.ok(!s.calls.some(x => x[0] === 'post'));
  await s.run('saveOutputName(true)');
  assert.equal(s.calls.find(x => x[0] === 'post')[3].title, '');
});
