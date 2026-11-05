import importlib.util
import json
import sqlite3
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


if __name__ == '__main__':
    unittest.main()
