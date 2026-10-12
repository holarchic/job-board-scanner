import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobscan import store
from jobscan.config import ConfigError
from jobscan.records import Board, Posting


def posting(job_id, board="acme", title="Account Executive"):
    return Posting(key=f"ashby:{board}:{job_id}", ats="ashby", board=f"ashby:{board}", company=board.title(),
                   title=title, location="London", url=f"https://jobs.ashbyhq.com/{board}/{job_id}",
                   description="Sell software.")


class ApplyFetchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open_db(Path(tmp.name))
        self.addCleanup(self.conn.close)

    def closed(self):
        return {r["key"]: r["closed"] for r in self.conn.execute("SELECT key, closed FROM postings")}

    def test_absent_postings_close_and_reseen_reopen(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2")), "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T11:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 0, "ashby:acme:2": 1})
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2")), "2026-10-11T12:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 0, "ashby:acme:2": 0})

    def test_valid_empty_board_closes_all_and_other_boards_untouched(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:beta", (posting("1", "beta"),), "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (), "2026-10-11T11:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 1, "ashby:beta:1": 0})

    def test_closure_does_not_depend_on_clock_order(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2")), "2026-10-11T12:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T09:00:00.000000+00:00")
        self.assertEqual(self.closed(), {"ashby:acme:1": 0, "ashby:acme:2": 1})

    def test_open_counts_every_open_role_per_board(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2", title="Engineer"), posting("3")),
                          "2026-10-11T10:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"), posting("2", title="Engineer")),
                          "2026-10-11T11:00:00.000000+00:00")
        store.apply_fetch(self.conn, "ashby:beta", (posting("1", "beta"),), "2026-10-11T10:00:00.000000+00:00")
        self.assertEqual(store.open_counts(self.conn, {"ashby:acme", "ashby:gone"}), {"ashby:acme": 2, "ashby:gone": 0})

    def test_unreadable_board_list_is_a_config_error(self):
        broken = sqlite3.connect(":memory:")  # no boards table: the read fails the way a damaged file would
        broken.row_factory = sqlite3.Row
        self.addCleanup(broken.close)
        with self.assertRaises(ConfigError):
            store.discovered_at(broken)
        for stamp in ["not a date", "2026-10-11T00:00:00"]:  # unparseable; no time zone
            store.replace_discovered(self.conn, [Board("ashby", "a", "a", "https://x/a")], stamp)
            with self.assertRaises(ConfigError, msg=stamp):
                store.discovered_at(self.conn)

    def test_unopenable_database_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "jobscan.sqlite").mkdir()
            with self.assertRaises(store.ConfigError):
                store.open_db(Path(d))

    def test_first_seen_survives_updates_and_input_hash_tracks_location(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), "2026-10-11T10:00:00.000000+00:00")
        moved = Posting(**{**vars(posting("1")), "location": "Remote - UK"})
        store.apply_fetch(self.conn, "ashby:acme", (moved,), "2026-10-11T11:00:00.000000+00:00")
        row = self.conn.execute("SELECT first_seen, input_hash FROM postings").fetchone()
        self.assertEqual(row["first_seen"], "2026-10-11T10:00:00.000000+00:00")
        self.assertEqual(row["input_hash"], store.input_hash(moved))
        self.assertNotEqual(store.input_hash(moved), store.input_hash(posting("1")))



T0 = "2026-10-11T10:00:00.000000+00:00"


class MigrationTest(unittest.TestCase):
    """Version 0 databases hold Ashby slugs as the boards file wrote them; version 1 case-folds them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def version_0(self, keys):
        conn = sqlite3.connect(self.home / "jobscan.sqlite")
        conn.executescript(store.SCHEMA)
        for key in keys:
            board = key.rsplit(":", 1)[0]
            conn.execute("INSERT INTO postings VALUES (?, 'ashby', ?, 'Clerk', 'AE', 'London', 'u', 'd', 'h', ?, ?, 0)",
                         (key, board, T0, T0))
            conn.execute("INSERT INTO tag_results VALUES (?, 'h', 'def', 'model', '\"yes\"', 'q', 1, 0)", (key,))
        conn.execute("INSERT INTO deliveries VALUES ('david', ?, 'digest.md')", (keys[0],))
        conn.commit()
        conn.close()

    def open(self):
        conn = store.open_db(self.home)
        self.addCleanup(conn.close)
        return conn

    def test_keys_boards_deliveries_and_cache_fold_together(self):
        self.version_0(["ashby:Clerk:1", "ashby:Clerk:2", "ashby:Clerk:3"])
        conn = self.open()
        rows = conn.execute("SELECT key, board FROM postings ORDER BY key").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(f"ashby:clerk:{n}", "ashby:clerk") for n in (1, 2, 3)])
        self.assertEqual([r[0] for r in conn.execute("SELECT posting_key FROM deliveries")], ["ashby:clerk:1"])
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT posting_key FROM tag_results")),
                         ["ashby:clerk:1", "ashby:clerk:2", "ashby:clerk:3"])
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        # The folded board behaves as one board: a read closes what it no longer lists and the delivered posting
        # stays delivered.
        store.apply_fetch(conn, "ashby:clerk", (posting("1", "clerk"), posting("2", "clerk")), T0)
        self.assertEqual(conn.execute("SELECT closed FROM postings WHERE key = 'ashby:clerk:3'").fetchone()[0], 1)
        pending = [s.posting.key for s in store.pending(conn, "david", {"ashby:clerk"})]
        self.assertEqual(pending, ["ashby:clerk:2"])

    def test_reopening_is_a_no_op(self):
        self.version_0(["ashby:Clerk:1"])
        self.open().close()
        conn = self.open()
        self.assertEqual([r[0] for r in conn.execute("SELECT key FROM postings")], ["ashby:clerk:1"])

    def test_collision_rolls_back_and_is_a_config_error(self):
        self.version_0(["ashby:Clerk:1", "ashby:clerk:1"])
        with self.assertRaises(ConfigError):
            store.open_db(self.home)
        conn = sqlite3.connect(self.home / "jobscan.sqlite")
        self.addCleanup(conn.close)
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT key FROM postings")),
                         ["ashby:Clerk:1", "ashby:clerk:1"])
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)


class NewerSchemaTest(unittest.TestCase):
    def test_database_from_a_newer_jobscan_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(Path(d) / "jobscan.sqlite")
            conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
            conn.close()
            with self.assertRaisesRegex(ConfigError, "newer"):
                store.open_db(Path(d))
            conn = sqlite3.connect(Path(d) / "jobscan.sqlite")
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)


class BoardsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open_db(Path(tmp.name))
        self.addCleanup(self.conn.close)

    def board(self, slug):
        return Board(ats="ashby", slug=slug, company=slug, api_url=f"https://api.ashbyhq.com/x/{slug}")

    def counts(self):
        return {r["id"]: r["not_found"] for r in self.conn.execute("SELECT id, not_found FROM boards")}

    def test_outcomes_reset_increment_or_leave_the_404_count(self):
        store.replace_discovered(self.conn, [self.board(s) for s in ("a", "b", "c")], T0)
        store.record_board_outcomes(self.conn, read=[], not_found=["ashby:a", "ashby:b"])
        store.record_board_outcomes(self.conn, read=["ashby:a"], not_found=["ashby:b"])
        store.record_board_outcomes(self.conn, read=[], not_found=[])  # c failed transiently: unchanged
        self.assertEqual(self.counts(), {"ashby:a": 0, "ashby:b": 2, "ashby:c": 0})

    def test_dead_boards_are_not_live_and_replacing_keeps_counts(self):
        store.replace_discovered(self.conn, [self.board(s) for s in ("a", "b")], T0)
        for _ in range(store.DEAD_AFTER):
            store.record_board_outcomes(self.conn, read=[], not_found=["ashby:a"])
        self.assertEqual([b.id for b in store.live_discovered(self.conn)], ["ashby:b"])
        store.replace_discovered(self.conn, [self.board(s) for s in ("a", "c")], T0)
        self.assertEqual(self.counts(), {"ashby:a": store.DEAD_AFTER, "ashby:c": 0})
        self.assertEqual([(b.id, b.company) for b in store.live_discovered(self.conn)], [("ashby:c", "c")])

    def test_pending_reads_only_the_given_boards(self):
        store.apply_fetch(self.conn, "ashby:acme", (posting("1"),), T0)
        store.apply_fetch(self.conn, "ashby:beta", (posting("1", "beta"),), T0)
        rows = store.pending(self.conn, "david", {"ashby:beta"})
        self.assertNotIsInstance(rows, list)
        self.assertEqual([s.posting.key for s in rows], ["ashby:beta:1"])


if __name__ == "__main__":
    unittest.main()
