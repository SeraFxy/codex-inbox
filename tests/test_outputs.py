import importlib.util
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ledger', Path(__file__).parents[1] / 'ledger.py')
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


class OutputCategoriesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ledger.SCHEMA)
        ledger.migrate_links(self.conn)
        self.key = 'codex:demo-session'
        self.add('https://example.feishu.cn/docx/ABCDEFGHIJKLMNOP', 1, 'Older document')
        self.add('https://example.feishu.cn/docx/ABCDEFGHIJKLMNOP#block', 2, 'Older document')
        self.add('/tmp/example-output.md', 20, 'Newer local file')
        self.add('https://example.com/report', 30, 'Newer website')
        self.add('https://github.com/example/project', 40, 'Code')
        self.add('https://github.com/example/project/pull/1', 50, 'Review')
        self.briefs = patch.object(ledger, 'session_briefs', return_value={})
        self.briefs.start()
        self.addCleanup(self.briefs.stop)
        self.addCleanup(self.conn.close)

    def add(self, url, ts, label, key=None):
        self.conn.execute('INSERT INTO links (session_key,url,ts,label,cat,doc,mr) VALUES (?,?,?,?,?,?,?)',
                          (key or self.key, url, ts, label, ledger.out_cat(url), ledger.feishu_doc(url), ledger.mr_ref(url)))

    def test_older_feishu_survives_preview_and_stays_ahead_of_favorites(self):
        links = [{'key': str(i), 'cat': 'file'} for i in range(12)]
        links.append({'key': 'document', 'cat': 'feishu'})
        preview = ledger.preview_outputs(links, {'0'}, 8)
        self.assertEqual(preview[0]['key'], 'document')
        self.assertIn('0', [x['key'] for x in preview])
        self.assertEqual(len(preview), 9)
        self.assertEqual(links[0]['key'], '0')

    def test_priority_applies_before_pagination_without_losing_outputs(self):
        first = ledger.search_outputs(self.conn, '', 'all', self.key, limit=2, priority=True)
        second = ledger.search_outputs(self.conn, '', 'all', self.key, limit=2, offset=2, priority=True)
        self.assertEqual(first['total'], 4)
        self.assertEqual(first['items'][0]['cat'], 'feishu')
        self.assertEqual(len({x['key'] for x in first['items'] + second['items']}), 4)
        self.assertEqual({c: first['counts'][c] for c in ('feishu', 'file', 'web', 'code')},
                         {'feishu': 1, 'file': 1, 'web': 1, 'code': 1})

    def test_category_search_is_scoped_to_session_and_excludes_mr_fallback(self):
        self.add('https://example.feishu.cn/wiki/QRSTUVWXYZABCDEF', 60, 'Other session', 'codex:other')
        result = ledger.search_outputs(self.conn, 'document', 'feishu', self.key, priority=True)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['cat'], 'feishu')
        self.assertEqual(ledger.search_outputs(self.conn, 'Review', 'all', self.key, priority=True)['total'], 0)
        self.assertEqual(ledger.search_outputs(self.conn, 'missing', 'feishu', self.key, priority=True)['total'], 0)

    def test_existing_output_page_keeps_chronological_order(self):
        result = ledger.search_outputs(self.conn, '', key=self.key)
        self.assertEqual([x['cat'] for x in result['items']], ['code', 'web', 'file', 'feishu'])


if __name__ == '__main__':
    unittest.main()
