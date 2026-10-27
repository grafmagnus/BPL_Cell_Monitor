import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from monitor import (Poll, Unit, collector_lock, load_config, open_database,
                     parse_unittable, poll_headend, store_cycle)

A = "00:25:18:04:7a:02"
B = "00:25:18:05:b6:a4"
HEADER = "UNIT TABLE\n ethmac | id | cell | age |\n---+---+---+---+\n"


class ParserTests(unittest.TestCase):
    def test_sample_crlf_and_case(self):
        rows = parse_unittable((HEADER + " 00:25:18:04:7A:02 | 1 | 15 | 4 |\n").replace("\n", "\r\n"))
        self.assertEqual(rows, [Unit(A, 1, 15, 4)])

    def test_valid_empty(self):
        self.assertEqual(parse_unittable(HEADER), [])

    def test_fail_closed(self):
        for text in ["command not found", HEADER + "bad row | 1 | 15 | 4 |", HEADER + "00:25:18:04:7a:02 | 1 | 15 | -1 |", "ethmac | id | cell | age |", HEADER + HEADER]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_unittable(text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_database(Path(self.tmp.name) / "test.db")
        self.cfg = {"headends": [{"name": "a"}, {"name": "b"}], "interval_seconds": 300, "retention_days": 56, "max_age": None}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def cycle(self, left=(), right=(), fail=False, ts=10000):
        return store_cycle(self.db, [Poll("a", "ok", ts+1, list(left)),
            Poll("b", "error" if fail else "ok", ts+2, list(right), error="timeout" if fail else None)], self.cfg, ts, ts+3)

    def state(self, mac=A):
        self.db.row_factory = sqlite3.Row
        return dict(self.db.execute("SELECT * FROM states WHERE mac=? ORDER BY cycle_id DESC LIMIT 1", (mac,)).fetchone())

    def test_duplicate_keeps_min_max_and_count(self):
        self.cycle([Unit(A, 1, 15, 4), Unit(A, 1, 15, 2)])
        self.assertEqual(self.state()["status"], "observed")
        self.assertEqual(tuple(self.db.execute("SELECT age,age_max,duplicates FROM observations").fetchone()), (2, 4, 1))

    def test_id_scoped_by_cell_and_headend(self):
        self.cycle([Unit(A, 1, 15, 1), Unit(B, 1, 16, 1)])
        self.assertEqual(self.state()["status"], "observed")
        self.cycle([Unit(A, 1, 15, 1)], [Unit(B, 1, 15, 1)], ts=10300)
        self.assertEqual(self.state(B)["status"], "observed")

    def test_same_cell_id_collision(self):
        self.cycle([Unit(A, 1, 15, 1), Unit(B, 1, 15, 1)])
        self.assertEqual(self.state()["status"], "conflict")
        self.assertEqual(self.state(B)["status"], "conflict")

    def test_multiple_assignments(self):
        self.cycle([Unit(A, 1, 15, 1)], [Unit(A, 1, 15, 1)])
        self.assertEqual(self.state()["status"], "conflict")
        self.assertIsNone(self.state()["headend"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)

    def test_same_mac_two_cells(self):
        self.cycle([Unit(A, 1, 15, 1), Unit(A, 1, 16, 1)])
        self.assertEqual(self.state()["status"], "conflict")

    def test_missing_vs_unknown_and_partial_observed(self):
        self.cycle([Unit(A, 1, 15, 1)])
        self.cycle(fail=True, ts=10300)
        self.assertEqual(self.state()["status"], "unknown")
        self.cycle(ts=10600)
        self.assertEqual(self.state()["status"], "missing")
        self.cycle([Unit(A, 1, 16, 1)], fail=True, ts=10900)
        self.assertEqual(self.state()["status"], "observed")
        self.assertEqual(self.state()["coverage_complete"], 0)

    def test_stale_and_fresh_selection(self):
        self.cfg["max_age"] = 10
        self.cycle([Unit(A, 1, 15, 11)])
        self.assertEqual(self.state()["status"], "stale")
        self.cycle([Unit(A, 1, 15, 11)], [Unit(A, 2, 16, 1)], ts=10300)
        self.assertEqual(self.state()["headend"], "b")

    def test_retention_cascades(self):
        self.cycle([Unit(A, 1, 15, 1)], ts=10000)
        self.cycle([Unit(A, 1, 16, 1)], ts=10000 + 57*86400)
        for table in ["cycles", "observations", "states"]:
            self.assertEqual(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_poll_set_validation(self):
        with self.assertRaises(ValueError):
            store_cycle(self.db, [], self.cfg)
        self.assertEqual(self.db.execute("SELECT count(*) FROM cycles").fetchone()[0], 0)

    def test_atomic_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.cycle([Unit(None, 1, 15, 1)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM cycles").fetchone()[0], 0)

    def test_exclusive_lock(self):
        path = Path(self.tmp.name) / "lock.db"
        with collector_lock(path):
            with self.assertRaises(RuntimeError):
                with collector_lock(path):
                    pass
        with collector_lock(path):
            pass


class PollTests(unittest.TestCase):
    def test_retry_then_success(self):
        cfg = {"retries": 1, "retry_delay_seconds": 0}
        with patch("monitor.ssh_output", side_effect=[TimeoutError("timeout"), HEADER]):
            result = poll_headend({"name": "a", "ssh_host": "alias"}, cfg)
        self.assertEqual((result.status, result.attempts), ("ok", 2))

    def test_bad_output_is_failure(self):
        with patch("monitor.ssh_output", return_value="broken"):
            result = poll_headend({"name": "a", "ssh_host": "alias"}, {"retries": 0})
        self.assertEqual(result.status, "error")

    def test_config_paths_and_unsafe_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            cfg = {"headends": [{"name": "a", "ssh_host": "root@10.113.40.72"}]}
            path.write_text(json.dumps(cfg))
            self.assertEqual(load_config(path)["database"], str((Path(directory) / "data" / "bpl.sqlite3").resolve()))
            cfg["headends"][0]["ssh_host"] = "-oProxyCommand=bad"
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
