import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ledger', Path(__file__).parents[1] / 'ledger.py')
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


class MessageFavoritesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'ledger.db'
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ledger.SCHEMA)
        self.addCleanup(self.conn.close)
        self.key = 'codex:example'
        self.body = 'First paragraph.\n\nSecond paragraph.'
        self.msg = {'messageId': ledger.history_id('it', 100, self.body), 'kind': 'it', 'ts': 100, 'text': self.body}

    def test_whole_message_and_distinct_excerpts_survive_reopen_without_transcript(self):
        choices = [self.msg, dict(self.msg, text='First paragraph.', excerpt=True),
                   dict(self.msg, text='Second paragraph.', excerpt=True)]
        for at, arg in enumerate(choices + [choices[1]], 1):
            self.assertTrue(ledger.save_message_favorite(self.conn, self.key, arg, at)[0])
        with sqlite3.connect(self.db) as reopened:
            reopened.row_factory = sqlite3.Row
            saved = ledger.message_favorites(reopened, self.key)
        self.assertEqual([(row['text'], row['excerpt']) for row in saved],
                         [('Second paragraph.', True), ('First paragraph.', True), (self.body, False)])
        self.assertTrue(all(row['messageId'] == self.msg['messageId'] for row in saved))

    def test_removing_excerpt_does_not_remove_whole_message_or_cross_session(self):
        other = 'claude:example'
        for key in (self.key, other):
            self.assertTrue(ledger.save_message_favorite(self.conn, key, self.msg, 100)[0])
        excerpt = dict(self.msg, text='First paragraph.', excerpt=True)
        self.assertTrue(ledger.save_message_favorite(self.conn, self.key, excerpt, 101)[0])
        target = ledger.message_favorites(self.conn, self.key)[0]
        remove = {'on': False, 'id': target['id']}
        ledger.save_message_favorite(self.conn, other, remove, 102)
        self.assertEqual(len(ledger.message_favorites(self.conn, self.key)), 2)
        ledger.save_message_favorite(self.conn, self.key, remove, 103)
        with patch.object(ledger, 'session_briefs', return_value={
            self.key: {'title': 'Example Codex'}, other: {'title': 'Example Claude'},
        }):
            all_saved = ledger.message_favorites(self.conn)
        self.assertEqual({(row['sessionKey'], row['title'], row['text']) for row in all_saved},
                         {(self.key, 'Example Codex', self.body), (other, 'Example Claude', self.body)})


if __name__ == '__main__':
    unittest.main()


class MessageMarksTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ledger.SCHEMA)
        self.addCleanup(self.conn.close)
        self.key = 'codex:example'
        body = 'Organizations are the boundary. Each user belongs to one.'
        msg = {'messageId': ledger.history_id('it', 100, body), 'kind': 'it', 'ts': 100, 'text': body}
        self.assertTrue(ledger.save_message_favorite(self.conn, self.key, msg, 1)[0])
        self.fav = ledger.message_favorites(self.conn, self.key)[0]
        self.assertEqual(self.fav['marks'], [])

    def mark(self, marks, key=None, ident=None):
        return ledger.save_message_marks(self.conn, key or self.key, {'id': ident or self.fav['id'], 'marks': marks})

    def test_marks_are_saved_sorted_and_come_back_with_the_favorite(self):
        later, first = {'start': 32, 'end': 41, 'text': 'Each user'}, {'start': 0, 'end': 13, 'text': 'Organizations'}
        self.assertEqual(self.mark([later, first]), (True, '已标黄'))
        self.assertEqual(ledger.message_favorites(self.conn)[0]['marks'], [first, later])
        self.assertEqual(self.mark([first]), (True, '已取消标黄'))
        self.assertEqual(self.mark([]), (True, '已取消标黄'))
        self.assertEqual(ledger.message_favorites(self.conn, self.key)[0]['marks'], [])

    def test_bad_marks_and_other_sessions_are_rejected_without_changing_saved_ones(self):
        good = [{'start': 0, 'end': 13, 'text': 'Organizations'}]
        self.assertTrue(self.mark(good)[0])
        for bad in ([{'start': 5, 'end': 5, 'text': 'x'}], [{'start': 0, 'end': 13, 'text': ' '}],
                    [{'start': 0, 'end': 10, 'text': 'a'}, {'start': 9, 'end': 12, 'text': 'b'}],
                    [{'start': '0', 'end': 3, 'text': 'Org'}], 'all', [None]):
            with self.subTest(bad=bad):
                self.assertFalse(self.mark(bad)[0])
        self.assertFalse(self.mark(good, key='codex:other')[0])
        self.assertFalse(self.mark(good, ident='missing')[0])
        self.assertEqual(ledger.message_favorites(self.conn, self.key)[0]['marks'], good)

    def test_unfavoriting_drops_the_marks_so_a_new_favorite_starts_clean(self):
        self.assertTrue(self.mark([{'start': 0, 'end': 13, 'text': 'Organizations'}])[0])
        self.assertTrue(ledger.save_message_favorite(self.conn, 'codex:other', {'on': False, 'id': self.fav['id']}, 2)[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM message_marks').fetchone()[0], 1)
        self.assertTrue(ledger.save_message_favorite(self.conn, self.key, {'on': False, 'id': self.fav['id']}, 3)[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM message_marks').fetchone()[0], 0)
