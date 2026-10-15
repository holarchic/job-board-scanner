import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobscan import backup, store
from jobscan.config import ConfigError

REAL_IDENTITY = backup.code_identity  # setUp patches it


class BackupTest(unittest.TestCase):
    """`store.open_db` copies the database before code it hasn't seen opens it, keeping two automatic copies."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.folder = self.home / backup.FOLDER
        self.code("old")
        self.open()  # a new install: no database to copy
        with self.connect() as conn:
            conn.execute("INSERT INTO deliveries (profile, posting_key, digest_path) VALUES ('ae', 'k1', 'd')")

    def code(self, identity):
        patcher = mock.patch.object(backup, "code_identity", lambda: identity.ljust(64, "0"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self):
        store.open_db(self.home).close()

    @contextlib.contextmanager
    def connect(self, path=None):
        """A connection that commits and closes on leaving the block."""
        conn = sqlite3.connect(path or self.home / "jobscan.sqlite")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def copies(self):
        return sorted(p.name for p in self.folder.glob("auto-*.sqlite"))

    def marker(self):
        return (self.folder / backup.MARKER).read_text().strip()

    def test_a_new_install_records_the_code_and_copies_nothing(self):
        self.assertEqual((self.copies(), self.marker()), ([], "old".ljust(64, "0")))
        self.open()
        self.assertEqual(self.copies(), [])

    def test_new_code_copies_the_database_once_before_using_it(self):
        self.code("new")
        self.open()
        (copy,) = self.copies()
        self.assertTrue(copy.endswith("-new00000.sqlite"))
        with self.connect(self.folder / copy) as conn:
            self.assertEqual(conn.execute("SELECT posting_key FROM deliveries").fetchall(), [("k1",)])
        self.open()
        self.assertEqual(self.copies(), [copy])
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.startswith(".")], [])

    def test_two_newest_copies_are_kept_and_hand_made_ones_never_touched(self):
        (self.folder / "jobscan-2026-10-15-pre-t016.sqlite").write_bytes(b"mine")
        with mock.patch.object(backup, "_stamp", lambda: "20261015T120000000000"):  # a frozen clock
            for identity in ("a1", "b2", "c3"):
                self.code(identity)
                self.open()
        self.assertEqual(self.copies(), ["auto-20261015T120000000000-b2000000.sqlite",
                                         "auto-20261015T120000000000-c3000000.sqlite"])
        self.assertEqual((self.folder / "jobscan-2026-10-15-pre-t016.sqlite").read_bytes(), b"mine")

    def test_a_copy_that_fails_stops_the_open_and_changes_nothing(self):
        before = (self.home / "jobscan.sqlite").read_bytes()
        self.code("new")
        with mock.patch.object(os, "link", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(ConfigError, "cannot copy the database.*No space left"):
                self.open()
        self.assertEqual(self.marker(), "old".ljust(64, "0"))
        self.assertEqual((self.home / "jobscan.sqlite").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [backup.MARKER])  # no copy, no temp file

    def test_an_unwritable_folder_stops_the_open(self):
        self.code("new")
        self.folder.chmod(0o500)
        self.addCleanup(self.folder.chmod, 0o700)
        with self.assertRaises(ConfigError):
            self.open()

    def test_the_copy_holds_the_database_as_it_was_before_a_migration(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO postings VALUES ('ashby:Clerk:1', 'ashby', 'ashby:Clerk', 'Clerk', 'AE',"
                         " 'London', 'u', '', 'h', 't', 't', 0)")
            conn.execute("PRAGMA user_version = 0")
        self.code("new")
        self.open()
        (copy,) = self.copies()
        with self.connect(self.folder / copy) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT key FROM postings").fetchall(), [("ashby:Clerk:1",)])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT key FROM postings").fetchall(), [("ashby:clerk:1",)])

    def test_the_identity_hashes_the_package_files(self):
        identity = REAL_IDENTITY.__wrapped__()
        self.assertRegex(identity, r"[0-9a-f]{64}")
        with mock.patch.object(Path, "read_bytes", lambda path: b"changed" if path.name == "scan.py" else b""):
            self.assertNotEqual(REAL_IDENTITY.__wrapped__(), identity)
