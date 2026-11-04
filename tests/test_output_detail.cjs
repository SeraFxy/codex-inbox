const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'board.html'), 'utf8');
const source = html.slice(html.indexOf('const dOut = {}'), html.indexOf('const dMrAll = {}'));
const cats = html.match(/const CATS = .*;/)[0];

function setup() {
  const requests = [], nodes = new Map();
  for (const id of ['dout-cats', 'dout-summary', 'dout-list', 'dout-more', 'dout-search'])
    nodes.set(id, { innerHTML: '', textContent: '', value: '', setAttribute() {} });
  const item = { key: 'demo', details: { linksTotal: 104, linksCounts: { feishu: 2, file: 101, web: 1, code: 0 }, links: [
    { key: 'file', cat: 'file' }, { key: 'doc', cat: 'feishu' }
  ] } };
  const ctx = vm.createContext({
    requests, selKey: 'demo', tab: 'board', setTimeout, clearTimeout, console,
    document: { getElementById: id => nodes.get(id) },
    findItem: key => key === 'demo' ? item : null,
    favFirst: list => [...list].sort((a, b) => Number(b.key === 'file') - Number(a.key === 'file')),
    linkList: list => list.map(x => x.key).join(','), num: n => String(n), clip: () => 1,
    esc: s => String(s).replace(/</g, '&lt;'), ico: () => '', btn: (text, action) => text + ':' + action,
    toast() {},
    fetch: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject }))
  });
  vm.runInContext(cats + '\n' + source, ctx);
  return { ctx, requests, nodes, run: code => vm.runInContext(code, ctx) };
}
const response = (items, total = items.length, counts = { feishu: total }) => ({ ok: true, json: async () => ({ items, total, counts }) });


test('collapsed list shows old Feishu documents before a favorite file and exposes all category counts', () => {
  const s = setup();
  assert.equal(s.run("outRows('demo')"), 'doc,file');
  const markup = s.run("outSec(findItem('demo'))");
  assert.match(markup, /飞书文档<span class="n">2/);
  assert.match(markup, /本机文件<span class="n">101/);
  assert.match(markup, /全部<span class="n">104/);
  assert.match(markup, /aria-label="搜索当前分类的产出物"/);
});

test('category and keyword query the complete session; late responses cannot overwrite the latest selection', async () => {
  const s = setup();
  s.run("dOutCat.demo='feishu'; dOutQ.demo='older document'");
  const older = s.run("expandOut('demo')");
  assert.match(s.requests[0].url, /cat=feishu&q=older%20document/);
  s.run("dOutCat.demo='file'; dOutQ.demo=''");
  const latest = s.run("expandOut('demo')");
  s.requests[1].resolve(response([{ key: 'file-result', cat: 'file' }], 1, { file: 1 }));
  await latest;
  s.requests[0].resolve(response([{ key: 'obsolete-doc', cat: 'feishu' }]));
  await older;
  assert.equal(s.run('dOut.demo.items[0].key'), 'file-result');
  assert.equal(s.nodes.get('dout-list').innerHTML, 'file-result');
});

test('pagination loads the remaining results instead of silently stopping at a fixed limit', async () => {
  const s = setup();
  const first = s.run("expandOut('demo')");
  s.requests[0].resolve(response(Array.from({ length: 100 }, (_, i) => ({ key: 'item-' + i, cat: 'file' })), 104, { file: 104 }));
  await first;
  assert.match(s.run("outMore('demo')"), /再加载 4 个/);
  const more = s.run("expandOut('demo',false,true)");
  assert.match(s.requests[1].url, /offset=100/);
  s.requests[1].resolve(response(Array.from({ length: 4 }, (_, i) => ({ key: 'next-' + i, cat: 'file' })), 104, { file: 104 }));
  await more;
  assert.equal(s.run('dOut.demo.items.length'), 104);
  assert.doesNotMatch(s.run("outMore('demo')"), /再加载/);
});

test('empty results and failed requests have explicit states; folding invalidates in-flight results', async () => {
  const s = setup();
  s.run("dOutCat.demo='code'");
  const empty = s.run("expandOut('demo')");
  s.requests[0].resolve(response([], 0, {}));
  await empty;
  assert.match(s.run("outRows('demo')"), /还没有这类产出物/);
  const failed = s.run("expandOut('demo')");
  s.requests[1].reject(new Error('offline'));
  await failed;
  assert.match(s.run("outMore('demo')"), /重试/);
  const pending = s.run("expandOut('demo')");
  s.run("foldOut('demo')");
  s.requests[2].resolve(response([{ key: 'late-result', cat: 'feishu' }]));
  await pending;
  assert.equal(s.run('dOut.demo'), undefined);
  assert.equal(s.run("outRows('demo')"), 'doc,file');
});
