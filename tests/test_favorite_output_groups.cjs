const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'board.html'), 'utf8');
const source = html.slice(html.indexOf('function outFavGroups('), html.indexOf('function favOn('));
const cats = html.match(/const CATS = .*;/)[0];
const web = { key: 'report', projectKey: 'alpha', project: 'Alpha', cat: 'web', railText: 'Report', url: 'https://example.com/report' };
const doc = { key: 'doc', projectKey: 'alpha', project: 'Alpha', cat: 'feishu', railText: 'Document', url: 'https://example.com/doc' };
const project = JSON.stringify(['project', 'alpha']);
const category = JSON.stringify(['group', 'alpha', 'cat:feishu']);

function setup(open = []) {
  const saved = {}, ctx = vm.createContext({
    web, doc, outFavOpen: new Set(open), clip: () => 1, ico: () => '', num: String,
    esc: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
    store: (k, v) => { saved[k] = [...v]; }, closeMenu() {}, renderRail() {}, document: { querySelectorAll: () => [] }
  });
  vm.runInContext(cats + '\n' + source, ctx);
  return { saved, run: code => vm.runInContext(code, ctx) };
}

test('same display label does not merge different projects; project order is respected', () => {
  const { run } = setup();
  const groups = run(`outFavGroups([web, doc, { ...doc, key: 'other', projectKey: 'beta' }], ['beta', 'alpha'])`);
  assert.deepEqual(Array.from(groups, g => g.key), ['beta', 'alpha']);
  assert.deepEqual(Array.from(groups, g => g.count), [1, 2]);
  assert.deepEqual(Array.from(groups[1].groups, g => g.label), ['飞书文档', '网页']);
});

test('topic and type groups are distinct, and Feishu documents lead mixed topics', () => {
  const { run } = setup();
  const groups = run(`outFavGroups([web, { ...web, key: 'topic-web', topic: { id: 3, name: '网页' } },
    { ...doc, topic: { id: 3, name: '网页' } }], [])`)[0].groups;
  assert.deepEqual(Array.from(groups, g => g.label), ['# 网页', '网页']);
  assert.deepEqual(Array.from(groups[0].items, x => x.key), ['doc', 'topic-web']);
});

test('project and category can be expanded independently and expansion survives reload', () => {
  const s = setup();
  assert.doesNotMatch(s.run('outFavTree([web, doc], [])'), /Document|飞书文档/);
  s.run(`toggleOutFavFold({ dataset: { outFavFold: ${JSON.stringify(project)} } })`);
  assert.match(s.run('outFavTree([web, doc], [])'), /飞书文档/);
  assert.doesNotMatch(s.run('outFavTree([web, doc], [])'), /Document/);
  s.run(`toggleOutFavFold({ dataset: { outFavFold: ${JSON.stringify(category)} } })`);
  const reopened = setup(s.saved['ledger.outfav.open']);
  assert.match(reopened.run('outFavTree([web, doc], [])'), /Document/);
  assert.doesNotMatch(reopened.run('outFavTree([web, doc], [])'), />Report</);
});
