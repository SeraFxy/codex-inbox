const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'board.html'), 'utf8');
const source = html.slice(html.indexOf('function outFavType('), html.indexOf('function favOn('));
const cats = html.match(/const CATS = .*;\nconst OUT_FAV_TYPES = .*;/)[0];
const web = { key: 'report', projectKey: 'alpha', project: 'Alpha', cat: 'web', railText: 'Report', url: 'https://example.com/report' };
const doc = { key: 'doc', projectKey: 'alpha', project: 'Alpha', cat: 'feishu', railText: 'Document', url: 'https://example.com/doc' };
const project = JSON.stringify(['project', 'alpha']);
const category = JSON.stringify(['group', 'alpha', 'cat:feishu']);

function setup(open = [], globals = {}) {
  const saved = {}, ctx = vm.createContext({
    web, doc, outFavOpen: new Set(open), clip: () => 1, ico: () => '', num: String,
    esc: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
    store: (k, v) => { saved[k] = [...v]; }, closeMenu() {}, renderRail() {}, document: { querySelectorAll: () => [] },
    ...globals
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
  const expanded = s.run('outFavTree([web, doc], [])');
  assert.match(expanded, /onclick="goOutFavList\(this\.dataset\.outFavScope\)"><span class="nm" title="飞书文档">飞书文档/);
  assert.doesNotMatch(s.run('outFavTree([web, doc], [])'), /Document/);
  s.run(`toggleOutFavFold({ dataset: { outFavFold: ${JSON.stringify(category)} } })`);
  const reopened = setup(s.saved['ledger.outfav.open']);
  assert.match(reopened.run('outFavTree([web, doc], [])'), /Document/);
  assert.doesNotMatch(reopened.run('outFavTree([web, doc], [])'), />Report</);
});


test('link type includes results inside topics and sidebar categories open the matching group', () => {
  const select = () => ({
    value: '',
    set innerHTML(markup) {
      this.options = Array.from(markup.matchAll(/<option value="([^"]*)">/g), m => ({ value: m[1] }));
      this.value = '';
    }
  });
  const controls = {
    project: select(), group: select(), type: select(), q: { value: '' },
    rows: {}, count: {}, manage: {}, clear: {}, table: {}, empty: {}, title: { focus() {} }
  };
  const scope = JSON.stringify(['group', 'alpha', 'cat:feishu']);
  const scopeButton = {
    dataset: { outFavScope: scope },
    setAttribute(name, value) { this[name] = value; }
  };
  const state = { outFavs: [
    doc, web,
    { ...doc, key: 'reviewed', railText: 'Reviewed document', topic: { id: 3, name: 'Review' } },
    { ...web, key: 'another', projectKey: 'beta', project: 'Beta', railText: 'Another page' }
  ] };
  const s = setup([], {
    state, outFavLabel: '', window: { scrollTo() {} },
    document: {
      getElementById: id => id === 'favorite-pane' ? {} : controls[id.replace('out-fav-list-', '')],
      querySelectorAll: () => [scopeButton]
    },
    qWords: q => q ? q.split(' ') : [], hl: title => title, outLink: (item, cls, title) => title,
    outFavLabels: () => [], outLabelEdit: () => '', listLabelChips: () => '',
    renderOutFavLabelFilters() {}, btn: () => '',
    setTab: (tab, filters) => s.run('renderOutFavList')(filters)
  });
  const nav = html.slice(html.indexOf('function goOutFavList('), html.indexOf('function renderOutFavLabelFilters('));
  const list = html.slice(html.indexOf('function syncOutFavRailSelection('), html.indexOf('function listLabelChips('));
  s.run(nav + list);
  s.run('renderOutFavList()');
  controls.type.value = 'feishu';
  s.run('renderOutFavList()');
  assert.equal(controls.count.textContent, '显示 2 / 4 个收藏');
  assert.match(controls.rows.innerHTML, /Reviewed document/);

  controls.project.value = 'beta';
  s.run('renderOutFavList()');
  assert.deepEqual(controls.type.options.map(o => o.value), ['', 'web']);
  controls.q.value = 'unrelated search';
  s.run('outFavLabel = "old label"');
  s.run('goOutFavList')(scope);
  assert.deepEqual([controls.project.value, controls.group.value, controls.type.value], ['alpha', 'none', 'feishu']);
  assert.equal(controls.q.value, '');
  assert.equal(s.run('outFavLabel'), '');
  assert.equal(controls.count.textContent, '显示 1 / 4 个收藏');
  assert.doesNotMatch(controls.rows.innerHTML, /Reviewed document/);
  assert.equal(scopeButton['aria-current'], 'true');
});
