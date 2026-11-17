const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'board.html'), 'utf8');
const source = [
  ['function esc(', 'function clip('],
  ['function btn(', 'function tagChip('],
  ['let popInit', 'function openPop('],
  ['function labelGroups(', 'function labelSec('],
  ['function labelValues(', 'function syncOutputLabelChoices('],
  ['function sessionLabelDraftChips(', 'function toggleOutFav(']
].map(([start, end]) => html.slice(html.indexOf(start), html.indexOf(end))).join('\n');

test('the session label picker adds saved labels and typed labels alongside the existing ones', async () => {
  const input = { value: '', addEventListener(name, handler) { this[name] = handler; }, focus() {} };
  const options = { buttons: [], querySelectorAll() { return this.buttons; } };
  const current = {};
  const nodes = { 'session-label-value': input, 'session-label-choices': options, 'session-label-current': current };
  const state = {
    labeled: [{ labels: ['Review'] }, { labels: ['Pricing'], onBoard: false }],
    outputLabels: { example: ['Release Notes'] }
  };
  let markup, saved;
  const context = vm.createContext({
    state, tagEdit: null, tab: 'board', historicTask: null, hLoaded: true,
    document: { getElementById: id => nodes[id], querySelector: () => input, querySelectorAll: () => [] },
    findItem: key => ({ key, labels: ['Review'] }),
    openPop: (anchor, content) => {
      markup = content;
      options.buttons = Array.from(content.matchAll(/class="btn sec xs" data-label="([^"]+)"( hidden)?/g),
        match => ({ dataset: { label: match[1] }, hidden: !!match[2] }));
      return input;
    },
    post: async (action, key, arg) => {
      saved = [action, key, JSON.parse(JSON.stringify(arg))];
      return { ok: true };
    },
    closeTag() {}, closeMenu() {}, load: async () => {}, ico: () => '', toast() {}
  });
  vm.runInContext(source, context);
  vm.runInContext('editSessionLabels("codex:sample", {})', context);

  assert.match(markup, /移除标签「Review」/);
  assert.doesNotMatch(markup.match(/<input id="session-label-value"[^>]*>/)[0], /\bvalue=/);
  input.value = '#pri';
  input.input();
  const matches = options.buttons.filter(button => !button.hidden);
  assert.deepEqual(matches.map(button => button.dataset.label), ['Pricing']);

  vm.runInContext('toggleSessionLabel', context)(matches[0]);
  assert.match(current.innerHTML, /#Review.*#Pricing/);
  assert.equal(input.value, '');
  assert.equal(vm.runInContext('popDirty()', context), true);

  input.value = 'release notes，New label';
  await vm.runInContext('saveSessionLabelEdit()', context);
  assert.deepEqual(saved, [
    'session_labels_edit', 'codex:sample', { add: ['Pricing', 'Release Notes', 'New label'], remove: [] }
  ]);
});
