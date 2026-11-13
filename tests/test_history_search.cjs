const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '..', 'board.html'), 'utf8');
const ctx = vm.createContext({});
const source = html.match(/const HIST_CLAUSE_BREAK = [\s\S]+?(?=function applyHistSearch\()/)?.[0];
vm.runInContext(source || '', ctx);

const query = '按空间总量收费很容易被规避';
const later = '一个存了五百份文件的共享目录算是一个独立空间吗？' + query + '。';
const earlier = '共享目录算一个独立空间吗？里面已经有几百份文件。';

test('a long phrase can recover an earlier question from the context of its exact hit', () => {
  const clues = ctx.histSearchClues(query, [later]);
  assert.equal(ctx.histRelatedPhrase(earlier, clues), '共享目录算一个独立空间吗');
  assert.equal(ctx.histRelatedPhrase('共享目录算存储成本，其他系统算一个独立空间吗？', clues), '');
});

test('long Chinese searches tolerate a small wording change without an exact hit', () => {
  const clues = ctx.histSearchClues('审批完以后保留合同版本', []);
  assert.equal(ctx.histRelatedPhrase('审批完之后要保留合同版本。', clues), '审批完之后要保留合同版本');
});

test('short terms and links keep literal matching', () => {
  assert.equal(ctx.histSearchClues('共享目录', [later]).length, 0);
  assert.equal(ctx.histSearchClues('https://example.com/很长的中文链接地址', [later]).length, 0);
});
