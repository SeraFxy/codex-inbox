import importlib.util
import json
import sqlite3
import threading
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('ledger', Path(__file__).parents[1] / 'ledger.py')
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


class FavoriteOutputOriginsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ledger.SCHEMA)
        ledger.migrate_links(self.conn)
        self.addCleanup(self.conn.close)

    def add(self, session, url, ts, label='Document'):
        self.conn.execute('INSERT INTO links (session_key,url,label,cat,doc,ts) VALUES (?,?,?,?,?,?)',
                          (session, url, label, ledger.out_cat(url), ledger.feishu_doc(url), ts))

    def favorites(self, keys, items=()):
        ledger.meta_set(self.conn, 'out_favs', json.dumps(keys))
        return ledger.board_out_favs(self.conn, 1000, items)

    def action(self, action, arg, key=""):
        app = ledger.App.__new__(ledger.App)
        app.conn, app.lock = self.conn, threading.RLock()
        return app.action({"action": action, "arg": arg, "key": key})

    def test_current_topic_project_wins_over_session_directory(self):
        key, url = 'codex:source', 'https://example.com/report.html'
        self.add(key, url, 900)
        self.conn.execute('INSERT INTO tags VALUES (?,?,?)', ('Analysis', 'Reports', ''))
        topic = {'id': 7, 'name': 'Review', 'project': 'Analysis'}
        [item] = self.favorites([url], [{'key': key, 'projectKey': 'Desktop', 'title': 'Example chat', 'topic': topic}])
        self.assertEqual((item['projectKey'], item['project'], item['topic']), ('Analysis', 'Reports', topic))
        self.assertEqual(item['sessionKey'], key)

    def test_old_source_keeps_saved_topic_and_project(self):
        key, url = 'claude:historical', 'https://example.com/older.html'
        self.add(key, url, 100)
        self.conn.execute('INSERT INTO sessions VALUES (?,?,?)', (key, 100, json.dumps({'cwd': '/tmp/source', 'custom_title': 'Old chat'})))
        t = ledger.insert_topic(self.conn, 'ArchivedProject', 'Archive topic', 100)
        self.conn.execute('INSERT INTO session_topics VALUES (?,?,?)', (key, t['id'], 100))
        [item] = self.favorites([url])
        self.assertEqual(item['projectKey'], 'ArchivedProject')
        self.assertEqual(item['topic']['name'], 'Archive topic')

    def test_shared_feishu_document_uses_one_latest_source(self):
        url = 'https://example.feishu.cn/docx/ABCDEFGHIJKLMNOP'
        self.add('codex:older', url, 100)
        self.add('codex:newer', url + '#anchor', 900)
        items = [{'key': 'codex:older', 'projectKey': 'Older', 'topic': None},
                 {'key': 'codex:newer', 'projectKey': 'Newer', 'topic': None}]
        [item] = self.favorites([ledger.feishu_doc(url)], items)
        self.assertEqual(item['projectKey'], 'Newer')
        self.assertEqual(item['sessionKey'], 'codex:newer')
        self.assertEqual(item['cat'], 'feishu')

    def test_older_source_with_only_prompt_history_has_project(self):
        key, url = 'claude:old', 'https://example.com/old.html'
        self.add(key, url, 100)
        self.conn.execute('INSERT INTO prompts VALUES (?,?,?,?,?,?,?,?)',
                          ('prompt:1', key, 'claude', 100, 'Review a report', 'History', '/tmp/history', 'Old review'))
        [item] = self.favorites([url])
        self.assertEqual(item['projectKey'], 'History')
        self.assertIsNone(item['topic'])

    def test_historical_hashtag_uses_existing_topic_without_creating_one(self):
        key, url = 'claude:historical', 'https://example.com/hashtag.html'
        self.add(key, url, 100)
        self.conn.execute('INSERT INTO sessions VALUES (?,?,?)', (key, 100, json.dumps({
            'cwd': '/tmp/project', 'hashtags': [{'name': 'Existing', 'kind': 'new'}, {'name': 'Unknown', 'kind': 'new'}]})))
        t = ledger.insert_topic(self.conn, 'project', 'Existing', 100)
        [item] = self.favorites([url])
        self.assertEqual(item['topic']['id'], t['id'])
        self.assertEqual(len(ledger.load_topics(self.conn)), 1)
        self.conn.execute('INSERT INTO session_topics VALUES (?,NULL,?)', (key, 100))
        [item] = self.favorites([url])
        self.assertIsNone(item['topic'])

    def test_batch_move_wins_over_new_source_and_follows_topic_rename(self):
        doc, page = 'https://example.feishu.cn/docx/ABCDEFGHIJKLMNOP', 'https://example.com/report'
        self.add('codex:first', doc, 100)
        self.add('claude:second', page, 200)
        keys = [ledger.feishu_doc(doc), page]
        self.favorites(keys)
        t = ledger.insert_topic(self.conn, 'Review', 'Weekly', 200)
        ok, _ = self.action('out_fav_place', {'keys': keys, 'project': 'Review', 'topic': t['id']})
        self.assertTrue(ok)

        self.add('claude:new', doc + '#anchor', 300)
        sources = [{'key': 'claude:new', 'projectKey': 'NewSource', 'topic': None}]
        self.assertTrue(self.action('topic_edit', {'id': t['id'], 'name': 'Renamed'})[0])
        moved = ledger.board_out_favs(self.conn, 1000, sources)
        self.assertEqual([(x['projectKey'], x['topic']['name'], x['manualPlace']) for x in moved],
                         [('Review', 'Renamed', True), ('Review', 'Renamed', True)])
        self.assertEqual(moved[0]['sessionKey'], 'claude:new')
        self.assertEqual(moved[0]['url'], doc + '#anchor')

        self.assertTrue(self.action('out_fav_place', {'keys': keys, 'auto': True})[0])
        automatic = ledger.board_out_favs(self.conn, 1000, sources)
        self.assertEqual(automatic[0]['projectKey'], 'NewSource')
        self.assertTrue(all(not x['manualPlace'] for x in automatic))

    def test_invalid_batch_does_not_move_existing_favorite_or_create_topic(self):
        page = 'https://example.com/report'
        self.add('codex:source', page, 100)
        self.favorites([page])
        t = ledger.insert_topic(self.conn, 'Other', 'Weekly', 200)
        for arg in ({'keys': [page, 'missing'], 'project': 'Review', 'name': 'New'},
                    {'keys': [page], 'project': 'Review', 'topic': t['id']}):
            self.assertFalse(self.action('out_fav_place', arg)[0])
        self.assertEqual([r['id'] for r in ledger.load_topics(self.conn)], [t['id']])
        [item] = ledger.board_out_favs(self.conn, 1000)
        self.assertFalse(item['manualPlace'])

    def test_new_topic_and_assignment_saved_together_and_deleted_topic_falls_back_to_type(self):
        page = 'https://example.com/report'
        self.add('codex:source', page, 100)
        self.favorites([page])
        self.assertTrue(self.action('out_fav_place', {'keys': [page], 'project': 'Review', 'name': 'Weekly'})[0])
        [item] = ledger.board_out_favs(self.conn, 1000)
        self.assertEqual(item['topic']['name'], 'Weekly')
        self.assertFalse(self.conn.in_transaction)

        self.assertTrue(self.action('topic_edit', {'id': item['topic']['id'], 'delete': True})[0])
        [item] = ledger.board_out_favs(self.conn, 1000)
        self.assertEqual(item['projectKey'], 'Review')
        self.assertIsNone(item['topic'])
        self.assertTrue(item['manualPlace'])


if __name__ == '__main__':
    unittest.main()
