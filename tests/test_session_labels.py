import json
import sqlite3
import unittest
from unittest.mock import patch

import ledger


class SessionLabelsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ledger.SCHEMA)
        ledger.migrate_links(self.conn)
        self.addCleanup(self.conn.close)
        for fn, result in (("codex_extras", ({}, {})), ("codex_threads", {}), ("codex_automations", {}),
                           ("claude_live", {}), ("claude_bangs", {})):
            mock = patch.object(ledger, fn, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)
        self.old = "codex:historic"
        self.other = "claude:other"
        self.conn.executemany("INSERT INTO prompts VALUES (?,?,?,?,?,?,?,?)", [
            ("1", self.old, "codex", 100, "Discuss the coverage matrix", "Example", "/tmp/example", "Review"),
            ("2", self.old, "codex", 200, "Send the draft", "Example", "/tmp/example", "Review"),
            ("3", self.other, "claude", 300, "Another coverage matrix", "Example", "/tmp/example", "Unrelated"),
        ])
        self.conn.commit()

    def test_label_search_returns_the_session_without_inflating_message_hits(self):
        ok, _ = ledger.save_session_labels(self.conn, self.old, [" #健康诊断 ", "Brand Fit", "brand fit"])
        self.assertTrue(ok)
        self.assertEqual(ledger.session_prompts(self.conn, self.old)["labels"], ["健康诊断", "Brand Fit"])
        for query in ("健康诊断", "#健康诊断", "BRAND fit", "Brand coverage"):
            with self.subTest(query=query):
                result = ledger.search_sessions(self.conn, query, cross=False, handoff=True)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["sessions"][0]["key"], self.old)
                self.assertEqual(result["sessions"][0]["hits"], 0)
                self.assertTrue(result["sessions"][0]["labelHit"])
        self.assertFalse(ledger.search_sessions(self.conn, "健康诊断", tool="claude", cross=False)["sessions"])
        self.assertTrue(ledger.save_session_labels(self.conn, self.old, [])[0])
        self.assertEqual(ledger.search_sessions(self.conn, "健康诊断", cross=False)["total"], 0)

    def test_label_search_finds_session_with_no_prompt_index(self):
        key = "codex:output-only"
        self.conn.execute("INSERT INTO links (session_key,url,label,ts,cat) VALUES (?,?,?,?,?)",
                          (key, "https://example.com/summary", "Summary", 201, "web"))
        self.assertTrue(ledger.save_session_labels(self.conn, key, ["Status digest"])[0])
        results = ledger.search_sessions(self.conn, "status digest", cross=False)["sessions"]
        self.assertEqual([(r["key"], r["count"], r["lastTs"]) for r in results], [(key, 0, 201)])
        item = ledger.history_task_detail(self.conn, ledger.DEFAULTS, key)
        self.assertEqual(item["details"]["linksTotal"], 1)

    def test_history_detail_uses_old_state_and_outputs_without_rejoining_queue(self):
        st = ledger.new_state("codex", "historic", "/tmp/history.jsonl")
        st.update(custom_title="Review", cwd="/tmp/example", last_ts=200, n_prompts=2, thread_source="user")
        self.conn.execute("INSERT INTO sessions VALUES (?,?,?)", (self.old, 200, json.dumps(st)))
        self.conn.execute("INSERT INTO links (session_key,url,label,ts,cat) VALUES (?,?,?,?,?)",
                          (self.old, "https://example.com/draft", "Draft", 201, "web"))
        self.conn.commit()
        self.assertTrue(ledger.save_session_labels(self.conn, self.old, ["Find draft"])[0])
        now = 50 * 86400
        item = ledger.history_task_detail(self.conn, ledger.DEFAULTS, self.old, now)
        self.assertEqual((item["title"], item["labels"], item["historical"]), ("Review", ["Find draft"], True))
        self.assertEqual(item["details"]["linksTotal"], 1)
        self.assertEqual(item["details"]["links"][0]["url"], "https://example.com/draft")
        board = ledger.build_board(self.conn, ledger.DEFAULTS, now)
        self.assertNotIn(self.old, [i["key"] for g in board["groups"] for i in g["items"]] +
                         [i["key"] for i in board["hidden"]])

    def test_prompt_only_history_has_detail_and_unknown_session_cannot_be_labeled(self):
        item = ledger.history_task_detail(self.conn, ledger.DEFAULTS, self.old)
        self.assertEqual(item["title"], "Review")
        self.assertEqual(item["details"]["nPrompts"], 2)
        self.assertEqual(item["last"], 200)
        self.assertIsNone(ledger.history_task_detail(self.conn, ledger.DEFAULTS, "codex:missing"))
        self.assertFalse(ledger.save_session_labels(self.conn, "codex:missing", ["orphan"])[0])


if __name__ == "__main__":
    unittest.main()
