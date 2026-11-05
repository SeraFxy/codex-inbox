import importlib.util
import json
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ledger', Path(__file__).parents[1] / 'ledger.py')
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


class PageTitleTest(unittest.TestCase):
    def test_content_heading_and_html_entities(self):
        from web_titles import parse_page_title
        self.assertEqual(parse_page_title('<h1>What AI knows<br>about <em>your company</em> &amp; you</h1>'),
                         'What AI knows about your company & you')

    def test_tab_title_and_metadata_are_fallbacks(self):
        from web_titles import parse_page_title
        self.assertEqual(parse_page_title('<title>Actual report title</title>'), 'Actual report title')
        self.assertEqual(parse_page_title('<title>Portal</title><meta property="og:title" content="Detailed report">'), 'Detailed report')
        self.assertEqual(parse_page_title('<title>Sign in</title><h1>Sign in to your account</h1>'), '')

    def test_hidden_script_headings_do_not_replace_the_title(self):
        from web_titles import parse_page_title
        self.assertEqual(parse_page_title('<title>Real title</title><script>"<h1>Fake</h1>"</script><h1 hidden>Hidden</h1>'), 'Real title')

    def test_private_urls_credentials_and_non_pages_are_not_requested(self):
        from web_titles import public_web_url
        for url in ['http://127.0.0.1/admin', 'http://[::1]/', 'http://169.254.169.254/latest/meta-data/',
                    'https://user:password@example.com/', 'file:///etc/passwd', 'https://example.com/download.zip',
                    'https://example.com/report?access_token=secret', 'https://example.com:8080/report', 'https://example.com/api/action']:
            with self.subTest(url=url), patch('web_titles.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]):
                self.assertFalse(public_web_url(url))

    def test_dns_to_private_addresses_and_redirects_are_rejected(self):
        from web_titles import PublicRedirects, public_web_url
        with patch('web_titles.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.10', 443))]):
            self.assertFalse(public_web_url('https://example.com/report'))
        with self.assertRaises(ValueError):
            PublicRedirects().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')

    def test_fragments_share_one_title_cache_key(self):
        from web_titles import canonical_web_url
        self.assertEqual(canonical_web_url('https://example.com/deck.html#slide-2'), 'https://example.com/deck.html')
        self.assertEqual(canonical_web_url('https://example.com/deck.html?lang=en#slide-2'), 'https://example.com/deck.html?lang=en')


class OutputNamesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ledger.SCHEMA)
        ledger.migrate_links(self.conn)
        self.url = 'https://example.com/deck.html#slide-1'
        self.key = 'codex:demo'
        self.conn.execute('INSERT INTO links (session_key,url,label,cat,ts) VALUES (?,?,?,?,?)',
                          (self.key, self.url, 'Link here', 'web', 1))
        self.app = ledger.App.__new__(ledger.App)
        self.app.conn, self.app.lock = self.conn, threading.RLock()
        self.briefs = patch.object(ledger, 'session_briefs', return_value={})
        self.briefs.start()
        self.addCleanup(self.briefs.stop)
        self.addCleanup(self.conn.close)

    def items(self, q=''):
        return ledger.search_outputs(self.conn, q, key=self.key)['items']

    def test_page_title_replaces_vague_label_and_is_searchable(self):
        self.conn.execute('INSERT INTO web_titles VALUES (?,?,?)', ('https://example.com/deck.html', 'Actual presentation title', 100))
        self.assertEqual(self.items()[0]['text'], 'Actual presentation title')
        self.assertEqual(self.items('presentation')[0]['url'], self.url)
        self.assertEqual(self.items()[0]['label'], 'Link here')

    def test_manual_name_wins_over_automatic_refresh_and_reset_restores_title(self):
        self.conn.execute('INSERT INTO web_titles VALUES (?,?,?)', ('https://example.com/deck.html', 'Automatic title', 100))
        ok, _ = self.app.action({'action': 'out_name', 'key': self.url, 'arg': {'title': 'My saved material title'}})
        self.assertTrue(ok)
        self.conn.execute('UPDATE web_titles SET title=?', ('Updated automatic title',))
        self.assertEqual(self.items('saved')[0]['text'], 'My saved material title')
        self.assertEqual(self.items()[0]['customTitle'], 'My saved material title')
        self.assertEqual(self.conn.execute('SELECT label FROM links').fetchone()[0], 'Link here')
        self.assertTrue(self.app.action({'action': 'out_name', 'key': self.url, 'arg': {'title': ''}})[0])
        self.assertEqual(self.items()[0]['text'], 'Updated automatic title')

    def test_same_saved_title_appears_in_favorites(self):
        ledger.meta_set(self.conn, 'out_favs', json.dumps([self.url]))
        self.app.action({'action': 'out_name', 'key': self.url, 'arg': {'title': 'Recognizable material'}})
        self.assertEqual(ledger.board_out_favs(self.conn, 100)[0]['text'], 'Recognizable material')

    def test_saved_name_survives_reopening_database(self):
        self.app.action({'action': 'out_name', 'key': self.url, 'arg': {'title': 'Persistent name'}})
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'ledger.db')
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.close()
            reopened = sqlite3.connect(path)
            reopened.row_factory = sqlite3.Row
            self.assertEqual(ledger.search_outputs(reopened, '', key=self.key)['items'][0]['text'], 'Persistent name')
            reopened.close()

    def test_unknown_outputs_cannot_be_renamed(self):
        self.assertFalse(self.app.action({'action': 'out_name', 'key': 'https://example.com/missing', 'arg': {'title': 'Oops'}})[0])


if __name__ == '__main__':
    unittest.main()
